# custom_components/googlefindmy/Auth/firebase_messaging/fcmregister.py
#
# firebase-messaging
# https://github.com/sdb9696/firebase-messaging
#
# MIT License
#
# Copyright (c) 2017 Matthieu Lemoine
# Copyright (c) 2023 Steven Beth
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from base64 import b64encode, urlsafe_b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, cast

from aiohttp import ClientSession, ClientTimeout
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from google.protobuf.json_format import MessageToDict, MessageToJson

from ._typing import (
    CredentialsUpdatedCallable,
    JSONDict,
    MutableJSONMapping,
)
from .const import (
    AUTH_VERSION,
    FCM_INSTALLATION,
    FCM_REGISTRATION,
    FCM_SEND_URL,
    GCM_CHECKIN_URL,
    GCM_REGISTER3_URL,
    GCM_REGISTER_URL,
    GCM_SERVER_KEY_B64,
    SDK_VERSION,
)
from .proto.android_checkin_pb2 import (
    DEVICE_CHROME_BROWSER,
    AndroidCheckinProto,
    ChromeBuildProto,
)
from .proto.checkin_pb2 import (
    AndroidCheckinRequest,
    AndroidCheckinResponse,
)

_logger = logging.getLogger(__name__)


@dataclass
class FcmRegisterConfig:
    """Configuration for FCM/GCM registration.

    Attributes:
        project_id: The Google Cloud project ID.
        app_id: The Firebase App ID.
        api_key: The API key for the Firebase project.
        messaging_sender_id: The numeric Messaging Sender ID (project number).
        bundle_id: The bundle ID for the application.
        chrome_id: The Chrome ID, defaults to 'org.chromium.linux'.
        chrome_version: The Chrome version string.
        vapid_key: The VAPID key for web push notifications.
        persistent_ids: A list of persistent IDs.
        heartbeat_interval_ms: The heartbeat interval in milliseconds.

    Notes:
        - `messaging_sender_id` must be the *numeric* Sender ID (project number).
        - `vapid_key` should generally remain the default (server key b64). When equal
          to GCM_SERVER_KEY_B64 we do **not** include it in the registration payload
          to avoid server errors.
        - If Google rejects the numeric sender with `PHONE_REGISTRATION_ERROR`, we
          automatically fall back to the legacy server key used by upstream tools.
    """

    project_id: str
    app_id: str
    api_key: str
    messaging_sender_id: str
    bundle_id: str = "receiver.push.com"
    chrome_id: str = "org.chromium.linux"
    chrome_version: str = "94.0.4606.51"
    vapid_key: str | None = GCM_SERVER_KEY_B64
    persistent_ids: list[str] | None = None
    heartbeat_interval_ms: int = 5 * 60 * 1000  # 5 mins

    def __post_init__(self) -> None:
        """Post-initialization hook to set default for persistent_ids."""
        if self.persistent_ids is None:
            self.persistent_ids = []


class FcmRegister:
    """Minimal client performing GCM check-in and FCM registration (async-first)."""

    CLIENT_TIMEOUT = ClientTimeout(total=100)

    def __init__(
        self,
        config: FcmRegisterConfig,
        credentials: MutableJSONMapping | None = None,
        credentials_updated_callback: (
            CredentialsUpdatedCallable[MutableJSONMapping] | None
        ) = None,
        *,
        http_client_session: ClientSession | None = None,
        log_debug_verbose: bool = False,
    ):
        """
        Initialize the FCM registration client.

        Args:
            config: An FcmRegisterConfig instance.
            credentials: Optional dictionary with existing credentials.
            credentials_updated_callback: Optional callback for when credentials are updated.
            http_client_session: Optional aiohttp ClientSession to reuse.
            log_debug_verbose: If True, enables verbose debug logging.
        """
        self.config = config
        self.credentials: MutableJSONMapping | None = credentials
        self.credentials_updated_callback: (
            CredentialsUpdatedCallable[MutableJSONMapping] | None
        ) = credentials_updated_callback

        self._log_debug_verbose = log_debug_verbose

        self._http_client_session: ClientSession | None = http_client_session
        self._local_session: ClientSession | None = None

    # ---------------------------------------------------------------------
    # Helpers (logging / URL handling / redaction)
    # ---------------------------------------------------------------------
    @staticmethod
    def _redact(value: Any, keep_tail: int = 6) -> str:
        """Return a redacted version of tokens/ids for safe logging."""
        s = str(value or "")
        if not s:
            return ""
        if len(s) <= keep_tail:
            return "•••"
        return f"•••{s[-keep_tail:]}"

    @staticmethod
    def _looks_like_html(text: str) -> bool:
        """Heuristically detect whether a response body contains HTML."""
        if not text:
            return False
        stripped = text.lstrip()
        head = stripped[:64].lower()
        if head.startswith("<!doctype") or head.startswith("<html"):
            return True
        lowered = stripped.lower()
        if "<html" in lowered and "<title" in lowered:
            return True
        return (
            "error 404" in lowered
            or "that’s an error" in lowered
            or "that's an error" in lowered
        )

    # ---------------------------------------------------------------------
    # GCM Check-in
    # ---------------------------------------------------------------------
    def _get_checkin_payload(
        self, android_id: int | None = None, security_token: int | None = None
    ) -> AndroidCheckinRequest:
        """
        Construct the protobuf payload for a GCM check-in request.

        Args:
            android_id: Optional Android ID from a previous check-in.
            security_token: Optional security token from a previous check-in.

        Returns:
            An initialized AndroidCheckinRequest message.
        """
        chrome = ChromeBuildProto()
        chrome.platform = ChromeBuildProto.Platform.PLATFORM_LINUX  # 3
        chrome.chrome_version = self.config.chrome_version
        chrome.channel = ChromeBuildProto.Channel.CHANNEL_STABLE  # 1

        checkin = AndroidCheckinProto()
        checkin.type = DEVICE_CHROME_BROWSER  # 3
        checkin.chrome_build.CopyFrom(chrome)

        payload = AndroidCheckinRequest()
        payload.user_serial_number = 0
        payload.checkin.CopyFrom(checkin)
        payload.version = 3
        if android_id and security_token:
            payload.id = int(android_id)
            payload.security_token = int(security_token)

        return payload

    async def gcm_check_in_and_register(self) -> dict[str, Any] | None:
        """Combined helper: check-in, then register against GCM."""
        options = await self.gcm_check_in()
        if not options:
            raise RuntimeError("Unable to register and check in to GCM")
        gcm_credentials = await self.gcm_register(options)
        return gcm_credentials

    async def gcm_check_in(
        self,
        android_id: int | None = None,
        security_token: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Perform the GCM check-in request with retries and exponential backoff.

        Args:
            android_id: Optional Android ID from a previous check-in.
            security_token: Optional security token from a previous check-in.

        Returns:
            A dictionary with check-in response data (including new android_id and
            security_token), or None on failure.
        """
        payload = self._get_checkin_payload(android_id, security_token)

        if self._log_debug_verbose:
            _logger.debug(
                "GCM check-in payload prepared (with%s credentials).",
                "" if (android_id and security_token) else "out",
            )

        max_attempts = 8
        content: bytes | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                async with self._session.post(
                    url=GCM_CHECKIN_URL,
                    headers={"Content-Type": "application/x-protobuf"},
                    data=payload.SerializeToString(),
                    timeout=self.CLIENT_TIMEOUT,
                ) as resp:
                    status = resp.status
                    if status == HTTPStatus.OK:
                        content = await resp.read()
                        break

                    text = await resp.text()
                    _logger.warning(
                        "GCM check-in failed (attempt %d/%d): url=%s, status=%s, body=%s",
                        attempt,
                        max_attempts,
                        GCM_CHECKIN_URL,
                        status,
                        text[:200],
                    )
                    # After a failure, retry **without** android_id/security_token once
                    payload = self._get_checkin_payload()
            except Exception as e:
                _logger.warning(
                    "GCM check-in error (attempt %d/%d) at url=%s: %s",
                    attempt,
                    max_attempts,
                    GCM_CHECKIN_URL,
                    e,
                )

            # Exponential backoff with light jitter
            if attempt < max_attempts:
                delay = min(1.5 * (2 ** (attempt - 1)), 30.0)
                delay *= 0.9 + 0.2 * secrets.randbits(4) / 15.0  # ±10% jitter
                await asyncio.sleep(delay)

        if not content:
            _logger.error(
                "Unable to check-in to GCM after %d attempts (url=%s)",
                max_attempts,
                GCM_CHECKIN_URL,
            )
            return None

        acir = AndroidCheckinResponse()
        acir.ParseFromString(content)

        if self._log_debug_verbose:
            msg = MessageToJson(acir, indent=4)
            _logger.debug("GCM check-in response (raw):\n%s", msg)

        parsed_response: JSONDict = MessageToDict(acir)
        return parsed_response

    # ---------------------------------------------------------------------
    # GCM Register (token)
    # ---------------------------------------------------------------------
    async def gcm_register(  # noqa: PLR0912,PLR0915
        self,
        options: dict[str, Any],
        retries: int = 8,
    ) -> dict[str, str] | None:
        """Obtain a GCM token with retries.

        Args:
            options: Dict containing ``androidId`` and ``securityToken`` from the
                check-in response.
            retries: Number of attempts before giving up.

        Returns:
            Dict with token/app_id/android_id/security_token on success, otherwise
            ``None``.

        Notes:
            Legacy upstream clients always used the legacy server key sender, which
            still succeeds instantly for most accounts. We keep that behaviour as
            the first attempt and only fall back to the configured numeric sender
            when Google rejects the legacy key (HTML/404 or explicit error). This
            matches observed production success rates while retaining support for
            modern projects that require the numeric sender.
        """
        gcm_app_id = f"wp:{self.config.bundle_id}#{uuid.uuid4()}"
        android_id = options["androidId"]
        security_token = options["securityToken"]

        headers = {
            "Authorization": f"AidLogin {android_id}:{security_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        sender_candidates: list[str] = []

        # Prefer the legacy server key because it consistently succeeds without
        # additional retries for existing deployments.
        sender_candidates.append(GCM_SERVER_KEY_B64)

        if (
            isinstance(self.config.messaging_sender_id, str)
            and self.config.messaging_sender_id
            and self.config.messaging_sender_id != GCM_SERVER_KEY_B64
        ):
            sender_candidates.append(self.config.messaging_sender_id)

        body = {
            "app": self.config.chrome_id,
            "X-subtype": gcm_app_id,
            "device": android_id,
            "sender": sender_candidates[0],
        }

        last_error: str | Exception | None = None
        sender_index = 0
        attempt = 1
        endpoints = (GCM_REGISTER3_URL, GCM_REGISTER_URL)
        endpoint_index = 0

        def sender_mode(sender: str) -> str:
            """Return a human readable label for the active sender."""

            if sender == GCM_SERVER_KEY_B64:
                return "legacy server key"
            if sender == self.config.messaging_sender_id:
                return "configured numeric sender"
            return "custom sender"

        def log_endpoint_switch(
            reason: str, current_index: int, next_index: int
        ) -> None:
            """Log when we switch between /register3 and /register endpoints."""

            current_indicator = (
                "/c2dm/register3"
                if endpoints[current_index] == GCM_REGISTER3_URL
                else "/c2dm/register"
            )
            next_indicator = (
                "/c2dm/register3"
                if endpoints[next_index] == GCM_REGISTER3_URL
                else "/c2dm/register"
            )
            _logger.warning(
                "GCM register switching endpoint %s -> %s due to %s; sender=%s (%s)",
                current_indicator,
                next_indicator,
                reason,
                body["sender"],
                sender_mode(body["sender"]),
            )

        while attempt <= retries:
            url = endpoints[endpoint_index]
            indicator = (
                "/c2dm/register3" if url == GCM_REGISTER3_URL else "/c2dm/register"
            )

            if self._log_debug_verbose:
                _logger.debug(
                    "GCM Registration request attempt %d/%d via %s: app=%s, X-subtype=%s, device=%s, sender=%s",
                    attempt,
                    retries,
                    indicator,
                    body["app"],
                    self._redact(body["X-subtype"]),
                    self._redact(body["device"]),
                    body["sender"],
                )

            try:
                async with self._session.post(
                    url=url,
                    headers=headers,
                    data=body,
                    timeout=self.CLIENT_TIMEOUT,
                ) as resp:
                    response_text = await resp.text()
                    content_type = resp.headers.get("Content-Type", "").lower()
                    status = resp.status
            except Exception as exc:  # network or aiohttp failure
                last_error = exc
                _logger.warning(
                    "GCM register request failed via %s (attempt %d/%d): %s",
                    indicator,
                    attempt,
                    retries,
                    exc,
                )
                if attempt < retries:
                    next_index = (endpoint_index + 1) % len(endpoints)
                    log_endpoint_switch(
                        f"exception={exc.__class__.__name__}",
                        endpoint_index,
                        next_index,
                    )
                    endpoint_index = next_index
                    await asyncio.sleep(1)
                attempt += 1
                continue

            html_like = "text/html" in content_type or self._looks_like_html(
                response_text
            )

            if status == HTTPStatus.NOT_FOUND or html_like:
                snippet = response_text[:200]
                last_error = f"Unexpected register response (status={status}, ctype={content_type}): {snippet}"
                fallback_triggered = False
                if sender_index + 1 < len(sender_candidates):
                    # Treat persistent HTML/404 responses as a signal to advance to the
                    # next sender candidate so we do not waste the entire retry budget
                    # on an endpoint that is not provisioned for the numeric sender.
                    previous_sender = body["sender"]
                    sender_index += 1
                    body["sender"] = sender_candidates[sender_index]
                    endpoint_index = 0
                    fallback_triggered = True
                    _logger.warning(
                        "GCM register received HTML/404 via %s; switching sender from %s (%s) to %s (%s)",
                        indicator,
                        previous_sender,
                        sender_mode(previous_sender),
                        body["sender"],
                        sender_mode(body["sender"]),
                    )
                if attempt < retries:
                    if not fallback_triggered:
                        next_index = (endpoint_index + 1) % len(endpoints)
                        log_endpoint_switch(
                            f"HTTP {status}", endpoint_index, next_index
                        )
                        endpoint_index = next_index
                    elif self._log_debug_verbose:
                        _logger.debug(
                            "GCM register resetting endpoint rotation after HTML/404 sender fallback"
                        )
                    await asyncio.sleep(1 if fallback_triggered else 0.5)
                else:
                    _logger.warning(
                        "GCM register 404/HTML via %s (status=%s); no retries left.",
                        indicator,
                        status,
                    )
                attempt += 1
                continue

            token: str | None = None
            error_code: str | None = None
            for line in response_text.splitlines():
                key, _, value = line.partition("=")
                lower_key = key.strip().lower()
                if lower_key == "token":
                    token = value.strip()
                    break
                if lower_key == "error":
                    error_code = value.strip().upper()

            if token:
                _logger.info(
                    "GCM register succeeded via %s on attempt %d/%d using sender=%s (%s)",
                    indicator,
                    attempt,
                    retries,
                    body["sender"],
                    sender_mode(body["sender"]),
                )
                return {
                    "token": token,
                    "app_id": gcm_app_id,
                    "android_id": android_id,
                    "security_token": security_token,
                }

            if error_code:
                last_error = f"Error={error_code}"
                if error_code == "PHONE_REGISTRATION_ERROR" and sender_index + 1 < len(
                    sender_candidates
                ):
                    _logger.debug(
                        "GCM register encountered PHONE_REGISTRATION_ERROR with sender=%s (%s)",
                        body["sender"],
                        sender_mode(body["sender"]),
                    )
                    sender_index += 1
                    body["sender"] = sender_candidates[sender_index]
                    label = (
                        "legacy server key"
                        if sender_candidates[sender_index] == GCM_SERVER_KEY_B64
                        else "configured numeric sender"
                    )
                    _logger.warning(
                        "GCM register error %s encountered; switching sender fallback to %s (sender=%s)",
                        error_code,
                        label,
                        body["sender"],
                    )
                    if attempt < retries:
                        await asyncio.sleep(1)
                    attempt += 1
                    continue

                _logger.warning(
                    "GCM register error via %s (attempt %d/%d): %s",
                    indicator,
                    attempt,
                    retries,
                    last_error,
                )
            else:
                snippet = response_text[:200]
                if html_like:
                    snippet += " [html]"
                last_error = f"Unexpected register response (status={status}, ctype={content_type}): {snippet}"
                _logger.warning(
                    "GCM register unexpected response via %s (attempt %d/%d): %s",
                    indicator,
                    attempt,
                    retries,
                    last_error,
                )

            if attempt < retries:
                rotation_reason = (
                    f"HTTP {status}" if status else last_error or "unknown"
                )
                if error_code:
                    rotation_reason = f"error_code={error_code}"
                next_index = (endpoint_index + 1) % len(endpoints)
                log_endpoint_switch(rotation_reason, endpoint_index, next_index)
                endpoint_index = next_index
                await asyncio.sleep(1)
            attempt += 1

        msg = f"Unable to complete GCM register after {retries} attempts"
        if isinstance(last_error, Exception):
            _logger.error(msg, exc_info=last_error)
        else:
            _logger.error("%s, last error was: %s", msg, last_error)
        return None

    # ---------------------------------------------------------------------
    # FCM (Install + Registration)
    # ---------------------------------------------------------------------
    async def fcm_install_and_register(
        self, gcm_data: dict[str, Any], keys: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        Perform FCM installation and registration in one step.

        Args:
            gcm_data: Credentials obtained from GCM registration.
            keys: Cryptographic keys generated for this session.

        Returns:
            A dictionary containing both installation and registration data, or None.
        """
        if installation := await self.fcm_install():
            registration = await self.fcm_register(gcm_data, installation, keys)
            return {
                "registration": registration,
                "installation": installation,
            }
        return None

    async def fcm_install(self) -> JSONDict | None:
        """
        Perform Firebase Installation to get an installation token.

        Returns:
            A dictionary with installation credentials (token, FID, etc.), or None.
        """
        fid = bytearray(secrets.token_bytes(17))
        # Replace the first 4 bits with the FID header 0b0111.
        fid[0] = 0b01110000 + (fid[0] % 0b00010000)
        fid64 = b64encode(fid).decode()

        hb_header = b64encode(
            json.dumps({"heartbeats": [], "version": 2}).encode()
        ).decode()
        headers = {
            "x-firebase-client": hb_header,
            "x-goog-api-key": self.config.api_key,
        }
        payload = {
            "appId": self.config.app_id,
            "authVersion": AUTH_VERSION,
            "fid": fid64,
            "sdkVersion": SDK_VERSION,
        }
        url = FCM_INSTALLATION + f"projects/{self.config.project_id}/installations"
        async with self._session.post(
            url=url,
            headers=headers,
            json=payload,
            timeout=self.CLIENT_TIMEOUT,
        ) as resp:
            if resp.status == HTTPStatus.OK:
                fcm_install = cast(JSONDict, await resp.json())
                return {
                    "token": fcm_install["authToken"]["token"],
                    "expires_in": int(
                        str(fcm_install["authToken"]["expiresIn"]).rstrip("s")
                    ),
                    "refresh_token": fcm_install["refreshToken"],
                    "fid": fcm_install["fid"],
                    "created_at": time.monotonic(),
                }
            else:
                text = await resp.text()
                _logger.error(
                    "Error during fcm_install at %s (status=%s): %s",
                    url,
                    resp.status,
                    text[:300],
                )
                return None

    async def fcm_refresh_install_token(self) -> JSONDict | None:
        """
        Refresh an expired FCM installation token.

        Returns:
            A dictionary with the new token and its expiry, or None.
        """
        hb_header = b64encode(
            json.dumps({"heartbeats": [], "version": 2}).encode()
        ).decode()
        if not self.credentials:
            raise RuntimeError("Credentials must be set to refresh install token")

        # Defensive access — log precisely which field is missing if any
        try:
            fcm_refresh_token = self.credentials["fcm"]["installation"]["refresh_token"]
            fid = self.credentials["fcm"]["installation"]["fid"]
        except KeyError as e:
            _logger.error("Cannot refresh FCM token: missing credentials key: %s", e)
            return None

        headers = {
            "Authorization": f"{AUTH_VERSION} {fcm_refresh_token}",
            "x-firebase-client": hb_header,
            "x-goog-api-key": self.config.api_key,
        }
        payload = {
            "installation": {"sdkVersion": SDK_VERSION, "appId": self.config.app_id}
        }

        url = (
            FCM_INSTALLATION
            + f"projects/{self.config.project_id}/installations/{fid}/authTokens:generate"
        )
        async with self._session.post(
            url=url,
            headers=headers,
            json=payload,
            timeout=self.CLIENT_TIMEOUT,
        ) as resp:
            if resp.status == HTTPStatus.OK:
                fcm_refresh = cast(JSONDict, await resp.json())
                return {
                    "token": fcm_refresh["token"],
                    "expires_in": int(str(fcm_refresh["expiresIn"]).rstrip("s")),
                    "created_at": time.monotonic(),
                }
            else:
                text = await resp.text()
                _logger.error(
                    "Error during fcm_refresh_install_token at %s (status=%s): %s",
                    url,
                    resp.status,
                    text[:300],
                )
                return None

    def generate_keys(self) -> dict[str, str]:
        """Generate public/private key pair and auth secret for FCM."""
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = private_key.public_key()

        serialized_private = private_key.private_bytes(
            encoding=serialization.Encoding.DER,  # asn1
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        serialized_public = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        return {
            "public": urlsafe_b64encode(serialized_public[26:]).decode("ascii"),
            "private": urlsafe_b64encode(serialized_private).decode("ascii"),
            "secret": urlsafe_b64encode(os.urandom(16)).decode("ascii"),
        }

    async def fcm_register(
        self,
        gcm_data: Mapping[str, Any],
        installation: Mapping[str, Any],
        keys: Mapping[str, Any],
        retries: int = 2,
    ) -> JSONDict | None:
        """
        Register the client with FCM to get the final FCM token.

        Args:
            gcm_data: Credentials from GCM registration.
            installation: Credentials from FCM installation.
            keys: Cryptographic keys for this session.
            retries: Number of retry attempts.

        Returns:
            FCM registration data dictionary, or None.
        """
        headers = {
            "x-goog-api-key": self.config.api_key,
            "x-goog-firebase-installations-auth": installation["token"],
        }
        # If vapid_key is the default do not send it here or it will error
        vapid_key = (
            self.config.vapid_key
            if self.config.vapid_key != GCM_SERVER_KEY_B64
            else None
        )
        payload = {
            "web": {
                "applicationPubKey": vapid_key,
                "auth": keys["secret"],
                "endpoint": FCM_SEND_URL + gcm_data["token"],
                "p256dh": keys["public"],
            }
        }
        url = FCM_REGISTRATION + f"projects/{self.config.project_id}/registrations"
        if self._log_debug_verbose:
            _logger.debug(
                "FCM registration data (url=%s): endpoint=%s…, appPubKey=%s, p256dh=%s…",
                url,
                (payload["web"]["endpoint"][:48] + "…"),
                bool(payload["web"]["applicationPubKey"]),
                self._redact(payload["web"]["p256dh"]),
            )

        last_error: str | Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                async with self._session.post(
                    url=url,
                    headers=headers,
                    json=payload,
                    timeout=self.CLIENT_TIMEOUT,
                ) as resp:
                    status = resp.status
                    if status == HTTPStatus.OK:
                        fcm = cast(JSONDict, await resp.json())
                        return fcm
                    else:
                        text = await resp.text()
                        _logger.error(
                            "Error during FCM register at %s (attempt %d/%d, status=%s): %s",
                            url,
                            attempt,
                            retries,
                            status,
                            text[:400],
                        )
            except Exception as e:
                last_error = e
                _logger.error(
                    "Error during FCM register at %s (attempt %d/%d)",
                    url,
                    attempt,
                    retries,
                    exc_info=e,
                )
                await asyncio.sleep(1)

        if isinstance(last_error, Exception):
            _logger.error(
                "FCM register ultimately failed at %s", url, exc_info=last_error
            )
        return None

    # ---------------------------------------------------------------------
    # Orchestration
    # ---------------------------------------------------------------------
    async def checkin_or_register(self) -> MutableJSONMapping:
        """Check in if you have credentials otherwise register as a new client.

        :return: The full credentials dict containing keys/gcm/fcm/config.
        """
        if self.credentials:
            try:
                gcm_response = await self.gcm_check_in(
                    self.credentials["gcm"]["android_id"],
                    self.credentials["gcm"]["security_token"],
                )
                if gcm_response:
                    return self.credentials
            except Exception as e:
                _logger.warning(
                    "Existing credentials check-in failed: %s; re-registering", e
                )

        self.credentials = await self.register()
        credentials = self.credentials
        if self.credentials_updated_callback and credentials is not None:
            try:
                self.credentials_updated_callback(credentials)
            except Exception as e:  # avoid caller breaking the flow
                _logger.debug("credentials_updated_callback raised: %s", e)

        if credentials is None:
            raise RuntimeError("Registration did not yield credentials")
        return credentials

    async def register(self) -> JSONDict:
        """Register GCM and FCM tokens for configured sender_id/app.

        Typically you would call `checkin_or_register()` instead of `register()`,
        which can reuse existing credentials when valid.
        """
        keys = self.generate_keys()

        gcm_data = await self.gcm_check_in_and_register()
        if gcm_data is None:
            raise RuntimeError(
                "Unable to establish subscription with Google Cloud Messaging."
            )
        self._log_verbose(
            "GCM subscription: %s",
            {**gcm_data, "token": self._redact(gcm_data.get("token"))},
        )

        fcm_data = await self.fcm_install_and_register(gcm_data, keys)
        if not fcm_data:
            raise RuntimeError("Unable to register with FCM")
        self._log_verbose(
            "FCM registration: %s", {"installation": "…", "registration": "…"}
        )

        res: dict[str, Any] = {
            "keys": keys,
            "gcm": gcm_data,
            "fcm": fcm_data,
            "config": {
                "bundle_id": self.config.bundle_id,
                "project_id": self.config.project_id,
                "vapid_key": self.config.vapid_key,
            },
        }
        self._log_verbose("Credential assembled (redacted).")
        _logger.info("Registered with FCM")
        return res

    def _log_verbose(self, msg: str, *args: object) -> None:
        """Log a debug message only if verbose logging is enabled."""
        if self._log_debug_verbose:
            _logger.debug(msg, *args)

    @property
    def _session(self) -> ClientSession:
        """
        Return the aiohttp session, creating one if it doesn't exist.
        """
        if self._http_client_session:
            return self._http_client_session
        if self._local_session is None:
            self._local_session = ClientSession()
        return self._local_session

    async def close(self) -> None:
        """Close the local aiohttp session if one was created."""
        session = self._local_session
        self._local_session = None
        if session:
            await session.close()
