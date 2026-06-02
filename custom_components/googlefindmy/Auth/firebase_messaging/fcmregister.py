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


class FcmRegisterHTTPError(RuntimeError):
    """Raised when an FCM/GCM endpoint returns a fatal HTTP status (401/404).

    Carries the numeric ``status`` so callers can classify the failure as an
    authentication error (401) or endpoint/credential rotation (404) and apply
    the appropriate retry budget. Inherits from ``RuntimeError`` to remain
    compatible with existing ``except RuntimeError`` callers, while allowing
    a dedicated ``except FcmRegisterHTTPError`` branch to escalate ahead of
    the generic transient-error handling.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


_FATAL_HTTP_STATUSES = (HTTPStatus.UNAUTHORIZED, HTTPStatus.NOT_FOUND)


_SHA1_HEX_LENGTH = 40


def _normalize_sha1_fingerprint(v: str) -> str:
    """Strip colons/spaces and validate a SHA-1 hex fingerprint."""
    v = v.replace(":", "").replace(" ", "").strip().lower()
    if len(v) != _SHA1_HEX_LENGTH or not all(c in "0123456789abcdef" for c in v):
        raise ValueError(f"Invalid SHA-1 fingerprint: {v!r}")
    return v


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
        - `android_cert_sha1` is the SHA-1 fingerprint of the Android signing
          certificate. When set together with `bundle_id`, both values are sent as
          ``X-Android-Package`` / ``X-Android-Cert`` headers on Firebase API calls
          so that API-key restrictions are satisfied.
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
    android_cert_sha1: str | None = None

    def __post_init__(self) -> None:
        """Post-initialization hook to set default for persistent_ids."""
        if self.persistent_ids is None:
            self.persistent_ids = []
        if self.android_cert_sha1 is not None:
            self.android_cert_sha1 = _normalize_sha1_fingerprint(
                self.android_cert_sha1
            )


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
    def _add_android_restriction_headers(self, headers: dict[str, str]) -> None:
        """Add X-Android-Package/Cert headers when configured."""
        if self.config.bundle_id:
            headers["X-Android-Package"] = self.config.bundle_id
        if self.config.android_cert_sha1:
            headers["X-Android-Cert"] = self.config.android_cert_sha1

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
                    if status in _FATAL_HTTP_STATUSES:
                        # 401/404 indicate invalid credentials or a moved
                        # endpoint — retrying with the same payload only
                        # multiplies the failure. Surface as fatal so the
                        # caller can rotate tokens or re-register.
                        raise FcmRegisterHTTPError(
                            f"GCM check-in fatal status {status}",
                            status=int(status),
                        )
                    # After a failure, retry **without** android_id/security_token once
                    payload = self._get_checkin_payload()
            except FcmRegisterHTTPError:
                # Propagate fatal HTTP status to the caller; do not swallow
                # into the generic transient-retry loop below.
                raise
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
            Upstream GoogleFindMyTools always uses the legacy server key
            (``GCM_SERVER_KEY_B64``) and never falls back to the configured
            numeric sender. We mirror that policy: the legacy key is the only
            sender candidate, regardless of HTTP 404, HTML responses, or
            ``PHONE_REGISTRATION_ERROR``. Earlier versions rotated to the
            numeric sender on rejection, which wasted the entire retry budget
            because the numeric sender returns persistent 404 for the affected
            account class (see commit ``ba186349`` for the upstream-alignment
            rationale).
        """
        gcm_app_id = f"wp:{self.config.bundle_id}#{uuid.uuid4()}"
        android_id = options["androidId"]
        security_token = options["securityToken"]

        headers = {
            "Authorization": f"AidLogin {android_id}:{security_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        body = {
            "app": self.config.chrome_id,
            "X-subtype": gcm_app_id,
            "device": android_id,
            "sender": GCM_SERVER_KEY_B64,
        }

        last_error: str | Exception | None = None
        attempt = 1

        while attempt <= retries:

            if self._log_debug_verbose:
                _logger.debug(
                    "GCM Registration request attempt %d/%d via /c2dm/register3: app=%s, X-subtype=%s, device=%s, sender=%s",
                    attempt,
                    retries,
                    body["app"],
                    self._redact(body["X-subtype"]),
                    self._redact(body["device"]),
                    body["sender"],
                )

            try:
                async with self._session.post(
                    url=GCM_REGISTER3_URL,
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
                    "GCM register request failed via /c2dm/register3 (attempt %d/%d): %s",
                    attempt,
                    retries,
                    exc,
                )
                if attempt < retries:
                    await asyncio.sleep(1)
                attempt += 1
                continue

            html_like = "text/html" in content_type or self._looks_like_html(
                response_text
            )

            if status == HTTPStatus.NOT_FOUND or html_like:
                snippet = response_text[:200]
                last_error = f"Unexpected register response (status={status}, ctype={content_type}): {snippet}"
                _logger.warning(
                    "GCM register 404/HTML via /c2dm/register3 (attempt %d/%d, status=%s)",
                    attempt,
                    retries,
                    status,
                )
                if attempt < retries:
                    await asyncio.sleep(1)
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
                    "GCM register succeeded via /c2dm/register3 on attempt %d/%d using sender=%s (legacy server key)",
                    attempt,
                    retries,
                    body["sender"],
                )
                return {
                    "token": token,
                    "app_id": gcm_app_id,
                    "android_id": android_id,
                    "security_token": security_token,
                }

            if error_code:
                last_error = f"Error={error_code}"
                if error_code == "PHONE_REGISTRATION_ERROR":
                    # Transient error — just retry with the same sender.
                    # Upstream GoogleFindMyTools treats this as transient and
                    # retries without switching sender, which eventually succeeds.
                    _logger.info(
                        "GCM register %s (transient, attempt %d/%d); "
                        "retrying with same sender=%s (legacy server key)",
                        error_code,
                        attempt,
                        retries,
                        body["sender"],
                    )
                else:
                    _logger.warning(
                        "GCM register error via /c2dm/register3 (attempt %d/%d): %s",
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
                    "GCM register unexpected response via /c2dm/register3 (attempt %d/%d): %s",
                    attempt,
                    retries,
                    last_error,
                )

            if attempt < retries:
                await asyncio.sleep(1)
            attempt += 1

        msg = f"Unable to complete GCM register after {retries} attempts"
        if isinstance(last_error, Exception):
            _logger.error(msg, exc_info=last_error)
        else:
            _logger.error("%s, last error was: %s", msg, last_error)
        # If the retry budget was exhausted on persistent 401/404 responses
        # from /c2dm/register3, the endpoint is fatal for this credential
        # set — surface as FcmRegisterHTTPError(status=…) so the caller can
        # run the dedicated auth retry budget (401, with token invalidation
        # via _invalidate_fcm_tokens) or the endpoint retry budget (404)
        # instead of treating it as a transient runtime error. Mirrors
        # _FATAL_HTTP_STATUSES used by gcm_check_in, fcm_install,
        # fcm_register, and fcm_refresh_install_token.
        if isinstance(last_error, str):
            for fatal_status in _FATAL_HTTP_STATUSES:
                marker = f"status={int(fatal_status)}"
                if marker in last_error:
                    raise FcmRegisterHTTPError(
                        f"GCM register fatal status {int(fatal_status)} "
                        f"(persisted after {retries} attempts)",
                        status=int(fatal_status),
                    )
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
        self._add_android_restriction_headers(headers)
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
                if resp.status in _FATAL_HTTP_STATUSES:
                    raise FcmRegisterHTTPError(
                        f"fcm_install fatal status {resp.status}",
                        status=int(resp.status),
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
            missing_key = getattr(e, "args", ("<unknown>",))[0]
            _logger.error(
                "Cannot refresh FCM token: missing credentials key; skipping refresh",
                extra={"missing_credentials_key": missing_key},
            )
            return None

        headers = {
            "Authorization": f"{AUTH_VERSION} {fcm_refresh_token}",
            "x-firebase-client": hb_header,
            "x-goog-api-key": self.config.api_key,
        }
        self._add_android_restriction_headers(headers)
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
                    "Error during fcm_refresh_install_token; response redacted.",
                    extra={
                        "request_url": url,
                        "status": resp.status,
                        "response_length": len(text),
                    },
                )
                if resp.status in _FATAL_HTTP_STATUSES:
                    raise FcmRegisterHTTPError(
                        f"fcm_refresh_install_token fatal status {resp.status}",
                        status=int(resp.status),
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
        self._add_android_restriction_headers(headers)
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
                        if status in _FATAL_HTTP_STATUSES:
                            # Retrying 401/404 with the same payload only
                            # multiplies the failure — propagate immediately.
                            raise FcmRegisterHTTPError(
                                f"FCM register fatal status {status}",
                                status=int(status),
                            )
            except FcmRegisterHTTPError:
                raise
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
                    "Existing credentials check-in failed; re-registering",
                    exc_info=e,
                )

        self.credentials = await self.register()
        credentials = self.credentials
        if self.credentials_updated_callback and credentials is not None:
            try:
                self.credentials_updated_callback(credentials)
            except Exception as e:  # avoid caller breaking the flow
                _logger.debug("credentials_updated_callback raised", exc_info=e)

        if credentials is None:
            raise RuntimeError("Registration did not yield credentials")
        return credentials

    async def _fallback_full_register(
        self, reason: str
    ) -> MutableJSONMapping:
        """Run a full ``register()`` as fallback and notify the callback."""
        _logger.warning("%s; falling back to full register()", reason)
        self.credentials = await self.register()
        if self.credentials_updated_callback and self.credentials:
            try:
                self.credentials_updated_callback(self.credentials)
            except Exception as e:
                _logger.debug(
                    "credentials_updated_callback raised", exc_info=e
                )
        if self.credentials is None:
            raise RuntimeError("Fallback registration did not yield credentials")
        return self.credentials

    async def reregister_keeping_identity(self) -> MutableJSONMapping:
        """Re-register FCM tokens while preserving GCM device identity.

        Performs a check-in with the existing android_id/security_token,
        then obtains fresh GCM and FCM tokens.  Falls back to full
        ``register()`` if the identity is no longer valid.

        :return: The full credentials dict with same android_id but fresh tokens.
        """
        if not self.credentials or "gcm" not in self.credentials:
            return await self._fallback_full_register(
                "No existing GCM identity"
            )

        android_id = self.credentials["gcm"]["android_id"]
        security_token = self.credentials["gcm"]["security_token"]

        # Step 1: Check-in with existing device identity
        try:
            gcm_response = await self.gcm_check_in(android_id, security_token)
        except Exception as e:
            _logger.debug("Check-in exception detail", exc_info=e)
            return await self._fallback_full_register(
                "Check-in with existing identity failed"
            )

        if not gcm_response:
            return await self._fallback_full_register(
                "Check-in returned empty response"
            )

        # Step 2: Re-register GCM token (uses same android_id/security_token)
        gcm_data = await self.gcm_register(gcm_response)
        if not gcm_data:
            raise RuntimeError(
                "GCM re-registration failed with existing identity"
            )

        # Step 3: Generate fresh keys and re-register FCM
        keys = self.generate_keys()
        fcm_data = await self.fcm_install_and_register(gcm_data, keys)
        if not fcm_data:
            raise RuntimeError("FCM re-registration failed")

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

        self.credentials = res
        if self.credentials_updated_callback:
            try:
                self.credentials_updated_callback(res)
            except Exception as e:
                _logger.debug(
                    "credentials_updated_callback raised", exc_info=e
                )

        _logger.info(
            "Re-registered FCM with existing device identity "
            "(android_id preserved)"
        )
        return res

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
