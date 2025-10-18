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
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import ClientSession, ClientTimeout
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from google.protobuf.json_format import MessageToDict, MessageToJson

from .const import (
    AUTH_VERSION,
    FCM_INSTALLATION,
    FCM_REGISTRATION,
    FCM_SEND_URL,
    GCM_CHECKIN_URL,
    GCM_REGISTER_URL,
    GCM_REGISTER3_URL,  # NEW: alternate endpoint constant
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
        credentials: dict | None = None,
        credentials_updated_callback: Callable[[dict[str, Any]], None] | None = None,
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
        self.credentials = credentials
        self.credentials_updated_callback = credentials_updated_callback

        self._log_debug_verbose = log_debug_verbose

        self._http_client_session = http_client_session
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
        """Heuristic check to decide if a response body is HTML (e.g., 404 page).

        Notes:
            We intentionally keep this robust because Google may return a branded
            HTML error page with status 200. We check for:
            - Leading '<!doctype' or '<html'
            - Presence of both '<html' and '<title>'
            - Known strings from Google error pages
        """
        if not text:
            return False
        t = text.lstrip()
        head = t[:64].lower()
        if head.startswith("<!doctype") or head.startswith("<html"):
            return True
        tl = t.lower()
        if "<html" in tl and "<title" in tl:
            return True
        if "error 404" in tl or "that’s an error" in tl or "that's an error" in tl:
            return True
        return False

    @staticmethod
    def _toggle_gcm_register_variant(url: str) -> str:
        """Toggle between the canonical /c2dm/register and /c2dm/register3 endpoints.

        Uses constants to avoid brittle path-suffix checks and to ensure we always
        flip exactly once between the two known endpoints.
        """
        if url == GCM_REGISTER_URL:
            return GCM_REGISTER3_URL
        if url == GCM_REGISTER3_URL:
            return GCM_REGISTER_URL
        # Defensive default: start from the base endpoint.
        return GCM_REGISTER_URL

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
            _logger.debug("GCM check-in payload prepared (with%s credentials).",
                          "" if (android_id and security_token) else "out")

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
                    if status == 200:
                        content = await resp.read()
                        break

                    text = await resp.text()
                    _logger.warning(
                        "GCM check-in failed (attempt %d/%d): url=%s, status=%s, body=%s",
                        attempt, max_attempts, GCM_CHECKIN_URL, status, text[:200],
                    )
                    # After a failure, retry **without** android_id/security_token once
                    payload = self._get_checkin_payload()
            except Exception as e:
                _logger.warning(
                    "GCM check-in error (attempt %d/%d) at url=%s: %s",
                    attempt, max_attempts, GCM_CHECKIN_URL, e,
                )

            # Exponential backoff with light jitter
            if attempt < max_attempts:
                delay = min(1.5 * (2 ** (attempt - 1)), 30.0)
                delay *= (0.9 + 0.2 * secrets.randbits(4) / 15.0)  # ±10% jitter
                await asyncio.sleep(delay)

        if not content:
            _logger.error("Unable to check-in to GCM after %d attempts (url=%s)",
                          max_attempts, GCM_CHECKIN_URL)
            return None

        acir = AndroidCheckinResponse()
        acir.ParseFromString(content)

        if self._log_debug_verbose:
            msg = MessageToJson(acir, indent=4)
            _logger.debug("GCM check-in response (raw):\n%s", msg)

        return MessageToDict(acir)

    # ---------------------------------------------------------------------
    # GCM Register (token)
    # ---------------------------------------------------------------------
    async def gcm_register(
        self,
        options: dict[str, Any],
        retries: int = 8,
    ) -> dict[str, str] | None:
        """
        Obtain a GCM token with retries and exponential backoff.

        Args:
            options: A dict containing 'androidId' and 'securityToken'.
            retries: The number of retry attempts.

        Returns:
            A minimal credential dict with token, app_id, etc., or None on failure.
            returns {"token": "...", "gcm_app_id": 123123, "android_id":123123,
                "security_token": 123123}
        """
        gcm_app_id = f"wp:{self.config.bundle_id}#{uuid.uuid4()}"
        android_id = options["androidId"]
        security_token = options["securityToken"]

        # NB: we purposefully do NOT send any 'Accept' header here.
        headers = {
            "Authorization": f"AidLogin {android_id}:{security_token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Android-GCM/1.5",
        }
        body = {
            "app": self.config.chrome_id,
            "X-subtype": gcm_app_id,
            "device": android_id,
            # IMPORTANT: Use **numeric** sender/project number, not server key b64.
            "sender": self.config.messaging_sender_id,
        }
        if self._log_debug_verbose:
            _logger.debug(
                "GCM Registration request (url=%s): app=%s, X-subtype=%s, device=%s, sender=%s",
                GCM_REGISTER_URL,
                body["app"],
                self._redact(body["X-subtype"]),
                self._redact(body["device"]),
                body["sender"],
            )

        url = GCM_REGISTER_URL
        tried_variant = False
        last_error: str | Exception | None = None

        # Non-retryable error codes returned by GCM register body (Error=<CODE>)
        NON_RETRYABLE = {
            "AUTHENTICATION_FAILED",
            "INVALID_PARAMETERS",
            "INVALID_SENDER",
            "MISSING_SENDER",
            "PHONE_REGISTRATION_ERROR",  # treat as deterministic for desktop-style clients
        }

        for attempt in range(1, retries + 1):
            try:
                async with self._session.post(
                    url=url,
                    headers=headers,
                    data=body,  # x-www-form-urlencoded
                    timeout=self.CLIENT_TIMEOUT,
                ) as resp:
                    response_status = resp.status
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                    response_text = await resp.text()

                # Detect HTML/404 error pages even when status==200.
                html_like = ("text/html" in content_type) or self._looks_like_html(response_text)
                if response_status == 404 or html_like:
                    if not tried_variant:
                        alt = self._toggle_gcm_register_variant(url)
                        _logger.warning(
                            "GCM register: 404/HTML received at %s (status=%s, ctype=%s); "
                            "toggling endpoint to %s and retrying",
                            url, response_status, content_type or "-", alt,
                        )
                        url = alt
                        tried_variant = True
                        # Quick retry without consuming an attempt slot
                        await asyncio.sleep(0.5)
                        continue

                # Parse body lines for structured keys
                token = None
                error_code = None
                for line in response_text.splitlines():
                    k, _, v = line.partition("=")
                    k = k.strip().lower()
                    if k == "token":
                        token = v.strip()
                    elif k == "error":
                        error_code = (v or "").strip().upper()

                if token:
                    _logger.info(
                        "GCM register succeeded via %s",
                        "/c2dm/register3" if url == GCM_REGISTER3_URL else "/c2dm/register",
                    )
                    return {
                        "token": token,
                        "app_id": gcm_app_id,
                        "android_id": android_id,
                        "security_token": security_token,
                    }

                if error_code:
                    last_error = f"Error={error_code}"
                    if error_code in NON_RETRYABLE:
                        _logger.error(
                            "GCM register returned non-retryable error at %s: %s — giving up",
                            url, last_error,
                        )
                        return None
                    _logger.warning(
                        "GCM register error at %s (attempt %d/%d): %s",
                        url, attempt, retries, last_error,
                    )
                else:
                    # Unexpected body without token/error
                    last_error = (
                        f"Unexpected register response (status={response_status}, "
                        f"ctype={content_type}): {response_text[:200]}"
                    )
                    _logger.warning(
                        "GCM register unexpected response at %s (attempt %d/%d): %s",
                        url, attempt, retries, last_error,
                    )

            except Exception as e:
                last_error = e
                _logger.warning(
                    "GCM register request failed at %s (attempt %d/%d): %s",
                    url, attempt, retries, e,
                )

            if attempt < retries:
                # Exponential backoff with cap and small jitter (network/transient only)
                delay = min(1.5 * (2 ** (attempt - 1)), 30.0)
                delay *= (0.9 + 0.2 * secrets.randbits(4) / 15.0)
                await asyncio.sleep(delay)

        msg = f"Unable to complete GCM register after {retries} attempts (last url={url})"
        if isinstance(last_error, Exception):
            _logger.error("%s", msg, exc_info=last_error)
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

    async def fcm_install(self) -> dict | None:
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
            if resp.status == 200:
                fcm_install = await resp.json()
                return {
                    "token": fcm_install["authToken"]["token"],
                    "expires_in": int(str(fcm_install["authToken"]["expiresIn"]).rstrip("s")),
                    "refresh_token": fcm_install["refreshToken"],
                    "fid": fcm_install["fid"],
                    "created_at": time.monotonic(),
                }
            else:
                text = await resp.text()
                _logger.error(
                    "Error during fcm_install at %s (status=%s): %s",
                    url, resp.status, text[:300],
                )
                return None

    async def fcm_refresh_install_token(self) -> dict | None:
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
        payload = {"installation": {"sdkVersion": SDK_VERSION, "appId": self.config.app_id}}

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
            if resp.status == 200:
                fcm_refresh = await resp.json()
                return {
                    "token": fcm_refresh["token"],
                    "expires_in": int(str(fcm_refresh["expiresIn"]).rstrip("s")),
                    "created_at": time.monotonic(),
                }
            else:
                text = await resp.text()
                _logger.error(
                    "Error during fcm_refresh_install_token at %s (status=%s): %s",
                    url, resp.status, text[:300],
                )
                return None

    def generate_keys(self) -> dict:
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
        gcm_data: dict,
        installation: dict,
        keys: dict,
        retries: int = 2,
    ) -> dict[str, Any] | None:
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
                    if status == 200:
                        fcm = await resp.json()
                        return fcm
                    else:
                        text = await resp.text()
                        _logger.error(
                            "Error during FCM register at %s (attempt %d/%d, status=%s): %s",
                            url, attempt, retries, status, text[:400],
                        )
            except Exception as e:
                last_error = e
                _logger.error(
                    "Error during FCM register at %s (attempt %d/%d)",
                    url, attempt, retries, exc_info=e,
                )
                await asyncio.sleep(1)

        if isinstance(last_error, Exception):
            _logger.error("FCM register ultimately failed at %s", url, exc_info=last_error)
        return None

    # ---------------------------------------------------------------------
    # Orchestration
    # ---------------------------------------------------------------------
    async def checkin_or_register(self) -> dict[str, Any]:
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
                _logger.warning("Existing credentials check-in failed: %s; re-registering", e)

        self.credentials = await self.register()
        if self.credentials_updated_callback:
            try:
                self.credentials_updated_callback(self.credentials)
            except Exception as e:  # avoid caller breaking the flow
                _logger.debug("credentials_updated_callback raised: %s", e)

        return self.credentials

    async def register(self) -> dict:
        """Register GCM and FCM tokens for configured sender_id/app.

        Typically you would call `checkin_or_register()` instead of `register()`,
        which can reuse existing credentials when valid.
        """
        keys = self.generate_keys()

        gcm_data = await self.gcm_check_in_and_register()
        if gcm_data is None:
            raise RuntimeError("Unable to establish subscription with Google Cloud Messaging.")
        self._log_verbose("GCM subscription: %s", {**gcm_data, "token": self._redact(gcm_data.get("token"))})

        fcm_data = await self.fcm_install_and_register(gcm_data, keys)
        if not fcm_data:
            raise RuntimeError("Unable to register with FCM")
        self._log_verbose("FCM registration: %s", {"installation": "…", "registration": "…"})

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
