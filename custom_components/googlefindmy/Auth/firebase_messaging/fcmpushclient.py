# custom_components/googlefindmy/Auth/firebase_messaging/fcmpushclient.py
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
import ssl
import struct
import time
import traceback
import random
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast
from collections.abc import Mapping

from aiohttp import ClientSession
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import load_der_private_key
from google.protobuf.json_format import MessageToJson
from google.protobuf.message import Message as RuntimeMessage
from http_ece import decrypt as http_decrypt

if TYPE_CHECKING:
    from custom_components.googlefindmy.protobuf_typing import MessageProto
else:
    MessageProto = RuntimeMessage

from .const import (
    MCS_HOST,
    MCS_PORT,
    MCS_SELECTIVE_ACK_ID,
    MCS_VERSION,
)
from .fcmregister import FcmRegister, FcmRegisterConfig
from ._typing import (
    CredentialsUpdatedCallable,
    JSONDict,
    MutableJSONMapping,
    NotificationContextT,
    OnNotificationCallable,
)
from .proto.mcs_pb2 import (  # pylint: disable=no-name-in-module
    Close,
    DataMessageStanza,
    HeartbeatAck,
    HeartbeatPing,
    IqStanza,
    LoginRequest,
    LoginResponse,
    SelectiveAck,
    StreamErrorStanza,
)

_logger = logging.getLogger(__name__)

# MCS Message Types and Tags
MCS_MESSAGE_TAG = {
    HeartbeatPing: 0,
    HeartbeatAck: 1,
    LoginRequest: 2,
    LoginResponse: 3,
    Close: 4,
    "MessageStanza": 5,
    "PresenceStanza": 6,
    IqStanza: 7,
    DataMessageStanza: 8,
    "BatchPresenceStanza": 9,
    StreamErrorStanza: 10,
    "HttpRequest": 11,
    "HttpResponse": 12,
    "BindAccountRequest": 13,
    "BindAccountResponse": 14,
    "TalkMetadata": 15,
}


class ErrorType(Enum):
    CONNECTION = 1
    READ = 2
    LOGIN = 3
    NOTIFY = 4


class FcmPushClientRunState(Enum):
    CREATED = (1,)
    STARTING_TASKS = (2,)
    STARTING_CONNECTION = (3,)
    STARTING_LOGIN = (4,)
    STARTED = (5,)
    STOPPING = (6,)
    STOPPED = (7,)


@dataclass
class FcmPushClientConfig:  # pylint:disable=too-many-instance-attributes
    """Configuration for FcmPushClient (worker-only; supervisor handles restarts)."""

    server_heartbeat_interval: int | None = 10
    """Time in seconds to request the server to send heartbeats"""

    client_heartbeat_interval: int | None = 20
    """Time in seconds to send heartbeats to the server"""

    # Kept for compatibility; used only on initial connect
    connection_retry_count: int = 5
    """Number of times to retry the initial connection before giving up."""

    # Not used by the worker anymore but kept for signature compatibility
    idle_reset_after: float = 90.0
    reset_interval: float = 3
    heartbeat_ack_timeout: float = 5
    monitor_interval: float = 1

    abort_on_sequential_error_count: int | None = 3
    """Number of sequential errors of the same type to wait before aborting.
       If set to None the client will not abort."""

    log_warn_limit: int | None = 5
    """Number of times to log specific warning messages before going silent for
       a specific warning type."""

    log_debug_verbose: bool = False
    """Set to True to log all message info including tokens."""

    # NEW: bounded writer shutdown (seconds) to avoid hanging on TLS close
    writer_close_timeout: float = 2.0


class FcmPushClient:  # pylint:disable=too-many-instance-attributes
    """Worker-only FCM client.
    - Establishes a single connection with initial retry.
    - Listens for messages until an error occurs or stop() is called.
    - No internal reset/monitor loops; outer supervisor restarts the client.
    """

    def __init__(
        self,
        callback: OnNotificationCallable[JSONDict, NotificationContextT],
        fcm_config: FcmRegisterConfig,
        credentials: MutableJSONMapping | None = None,
        credentials_updated_callback: (
            CredentialsUpdatedCallable[MutableJSONMapping] | None
        ) = None,
        *,
        callback_context: NotificationContextT | None = None,
        received_persistent_ids: list[str] | None = None,
        config: FcmPushClientConfig | None = None,
        http_client_session: ClientSession | None = None,
    ):
        """Initializes the receiver."""
        self.callback: OnNotificationCallable[JSONDict, NotificationContextT] = callback
        self.callback_context: NotificationContextT | None = callback_context
        self.fcm_config = fcm_config
        self.credentials: MutableJSONMapping | None = credentials
        self.credentials_updated_callback: (
            CredentialsUpdatedCallable[MutableJSONMapping] | None
        ) = credentials_updated_callback
        self.persistent_ids: list[str] = (
            list(received_persistent_ids)
            if received_persistent_ids is not None
            else []
        )
        self.config = config if config else FcmPushClientConfig()
        self._http_client_session: ClientSession | None = http_client_session
        self.register: FcmRegister | None = None

        # Instance-specific logger to avoid global side effects; honors log_debug_verbose.
        self.logger = logging.getLogger(f"{__name__}.FcmPushClient.{id(self)}")
        self.logger.propagate = True
        if self.config.log_debug_verbose:
            self.logger.setLevel(logging.DEBUG)

        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.do_listen = False
        self.sequential_error_counters: dict[ErrorType, int] = {}
        self.log_warn_counters: dict[str, int] = {}

        # State variables
        self.input_stream_id = 0
        self.last_input_stream_id_reported = -1
        self.first_message = True
        self.last_login_time: float = 0.0
        self.last_message_time: float = 0.0

        self.run_state: FcmPushClientRunState = FcmPushClientRunState.CREATED
        self.tasks: list[asyncio.Task[None]] = []

        # Locks
        self.stopping_lock: asyncio.Lock = asyncio.Lock()

    # ---- Logging helpers ----

    def _msg_str(self, msg: MessageProto) -> str:
        if self.config.log_debug_verbose:
            pretty_json = cast(str, MessageToJson(msg, indent=4))
            return f"{type(msg).__name__}\n{pretty_json}"
        return type(msg).__name__

    def _log_verbose(self, msg: str, *args: object) -> None:
        if self.config.log_debug_verbose:
            self.logger.debug(msg, *args)

    def _log_warn_with_limit(self, msg: str, *args: object) -> None:
        if msg not in self.log_warn_counters:
            self.log_warn_counters[msg] = 0
        if (
            self.config.log_warn_limit
            and self.config.log_warn_limit > self.log_warn_counters[msg]
        ):
            self.log_warn_counters[msg] += 1
            self.logger.warning(msg, *args)

    # ---- Connection helpers ----

    async def _do_writer_close(self) -> None:
        """Close the stream writer with a bounded wait; abort transport on timeout."""
        writer = self.writer
        self.writer = None
        if not writer:
            return
        try:
            try:
                writer.close()
            except Exception:
                # defensive: ignore close() errors
                pass
            try:
                # Bounded, graceful shutdown window
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=self.config.writer_close_timeout
                )
            except TimeoutError:
                # TLS shutdown timed out: hard-close
                self.logger.debug("SSL shutdown timed out; aborting transport")
                transport = writer.transport if hasattr(writer, "transport") else None
                if transport is not None:
                    try:
                        transport.abort()  # Immediate abort preferred
                    except Exception:
                        try:
                            transport.close()
                        except Exception:
                            pass
        except asyncio.CancelledError:
            # On cancel, still hard-close before propagating
            transport = writer.transport if hasattr(writer, "transport") else None
            if transport is not None:
                try:
                    transport.abort()
                except Exception:
                    try:
                        transport.close()
                    except Exception:
                        pass
            raise
        except Exception:
            # Keep old behavior: swallow all other errors on close.
            pass

    # protobuf varint32 helpers
    async def _read_varint32(self) -> int:
        reader = self.reader
        assert reader is not None, "StreamReader is not initialized"
        res = 0
        shift = 0
        while True:
            r = await reader.readexactly(1)
            byte_value = int(struct.unpack("B", r)[0])
            res |= (byte_value & 0x7F) << shift
            if (byte_value & 0x80) == 0:
                break
            shift += 7
        return res

    @staticmethod
    def _encode_varint32(x: int) -> bytes:
        if x == 0:
            return bytes(bytearray([0]))

        res = bytearray([])
        while x != 0:
            b = x & 0x7F
            x >>= 7
            if x != 0:
                b |= 0x80
            res.append(b)
        return bytes(res)

    @staticmethod
    def _make_packet(msg: MessageProto, include_version: bool) -> bytes:
        tag = MCS_MESSAGE_TAG[type(msg)]
        header = bytearray([MCS_VERSION, tag]) if include_version else bytearray([tag])
        payload = msg.SerializeToString()
        buf = bytes(header) + FcmPushClient._encode_varint32(len(payload)) + payload
        return buf

    async def _send_msg(self, msg: MessageProto) -> None:
        self._log_verbose("Sending packet to server: %s", self._msg_str(msg))
        buf = FcmPushClient._make_packet(msg, self.first_message)
        writer = self.writer
        assert writer is not None, "StreamWriter is not initialized"
        writer.write(buf)
        await writer.drain()

    async def _receive_msg(self) -> MessageProto | None:
        reader = self.reader
        assert reader is not None, "StreamReader is not initialized"

        if self.first_message:
            r = await reader.readexactly(2)
            version, tag = struct.unpack("BB", r)
            if version < MCS_VERSION and version != 38:
                raise RuntimeError(f"protocol version {version} unsupported")
            self.first_message = False
        else:
            r = await reader.readexactly(1)
            (tag,) = struct.unpack("B", r)
        size = await self._read_varint32()

        self._log_verbose("Received message with tag %s and size %s", tag, size)

        if not size >= 0:
            self._log_warn_with_limit("Unexpected message size %s", size)
            return None

        buf = await reader.readexactly(size)

        msg_class = next(iter([c for c, t in MCS_MESSAGE_TAG.items() if t == tag]))
        if not msg_class:
            self._log_warn_with_limit("Unexpected message tag %s", tag)
            return None
        if isinstance(msg_class, str):
            self._log_warn_with_limit("Unconfigured message class %s", msg_class)
            return None

        msg_type = cast(type[MessageProto], msg_class)
        payload = msg_type()
        payload.ParseFromString(buf)
        self._log_verbose("Received payload: %s", self._msg_str(payload))

        return payload

    async def _login(self) -> None:
        self.run_state = FcmPushClientRunState.STARTING_LOGIN

        now = time.time()
        self.input_stream_id = 0
        self.last_input_stream_id_reported = -1
        self.first_message = True
        self.last_login_time = now

        try:
            # Defensive access to credentials to avoid KeyError crashes
            if not isinstance(self.credentials, dict):
                raise ValueError("Missing credentials dictionary for FCM login")

            gcm_data = self.credentials.get("gcm")
            if not isinstance(gcm_data, dict):
                raise ValueError("'gcm' section is missing or invalid in credentials")

            android_id = gcm_data.get("android_id")
            security_token = gcm_data.get("security_token")
            if not android_id or not security_token:
                raise ValueError(
                    "android_id or security_token is missing from credentials"
                )

            req = LoginRequest()
            req.adaptive_heartbeat = False
            req.auth_service = LoginRequest.ANDROID_ID  # 2
            req.auth_token = security_token
            req.id = self.fcm_config.chrome_version
            req.domain = "mcs.android.com"
            req.device_id = f"android-{int(android_id):x}"
            req.network_type = 1
            req.resource = str(android_id)
            req.user = str(android_id)
            req.use_rmq2 = True
            req.setting.add(name="new_vc", value="1")
            req.received_persistent_id.extend(self.persistent_ids)
            if (
                self.config.server_heartbeat_interval
                and self.config.server_heartbeat_interval > 0
            ):
                req.heartbeat_stat.ip = ""
                req.heartbeat_stat.timeout = True
                req.heartbeat_stat.interval_ms = (
                    1000 * self.config.server_heartbeat_interval
                )

            await self._send_msg(req)
            self.logger.debug("Sent login request")
        except Exception as ex:
            self.logger.error("Received an exception logging in: %s", ex)
            if self._try_increment_error_count(ErrorType.LOGIN):
                # On worker, treat as fatal: stop listening and let supervisor restart
                self.do_listen = False

    @staticmethod
    def _decrypt_raw_data(
        credentials: Mapping[str, object],
        crypto_key_str: str,
        salt_str: str,
        raw_data: bytes,
    ) -> bytes:
        crypto_key = urlsafe_b64decode(crypto_key_str.encode("ascii"))
        salt = urlsafe_b64decode(salt_str.encode("ascii"))

        keys_section = credentials.get("keys")
        if not isinstance(keys_section, Mapping):
            raise ValueError("Credentials missing FCM key material")

        private_value = keys_section.get("private")
        secret_value = keys_section.get("secret")
        if not (isinstance(private_value, str) and isinstance(secret_value, str)):
            raise ValueError("Invalid key values in credential payload")

        der_data = urlsafe_b64decode(private_value.encode("ascii") + b"========")
        secret = urlsafe_b64decode(secret_value.encode("ascii") + b"========")
        privkey = load_der_private_key(
            der_data, password=None, backend=default_backend()
        )
        decrypted = cast(
            bytes,
            http_decrypt(
                raw_data,
                salt=salt,
                private_key=privkey,
                dh=crypto_key,
                version="aesgcm",
                auth_secret=secret,
            ),
        )
        return decrypted

    def _app_data_by_key(
        self, p: DataMessageStanza, key: str, do_not_raise: bool = False
    ) -> str:
        for x in p.app_data:
            if x.key == key:
                return x.value

        if do_not_raise:
            return ""
        raise RuntimeError(f"couldn't find in app_data {key}")

    def _handle_data_message(
        self,
        msg: DataMessageStanza,
    ) -> None:
        self.logger.debug(
            "Received data message Stream ID: %s, Last: %s, Status: %s",
            msg.stream_id,
            msg.last_stream_id_received,
            msg.status,
        )

        if (
            self._app_data_by_key(msg, "message_type", do_not_raise=True)
            == "deleted_messages"
        ):
            # The deleted_messages message does not contain data.
            return
        crypto_key = self._app_data_by_key(msg, "crypto-key")[3:]  # strip dh=
        salt = self._app_data_by_key(msg, "encryption")[5:]  # strip salt=
        subtype = self._app_data_by_key(msg, "subtype")
        if TYPE_CHECKING:
            assert self.credentials
        if subtype != self.credentials["gcm"]["app_id"]:
            self._log_warn_with_limit(
                "Subtype %s in data message does not match"
                + "app id client was registered with %s",
                subtype,
                self.credentials["gcm"]["app_id"],
            )
        if not self.credentials:
            return

        decrypted = self._decrypt_raw_data(
            self.credentials, crypto_key, salt, msg.raw_data
        )

        # Normalize decrypted payload to a dict (defensive against non-object JSON)
        decrypted_json: Any | None = None
        text: str | None = None
        try:
            text = decrypted.decode("utf-8")
        except Exception:
            text = None

        if text is not None:
            try:
                decrypted_json = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                decrypted_json = None

        if isinstance(decrypted_json, dict):
            ret_val = cast(JSONDict, decrypted_json)
        elif decrypted_json is not None:
            self._log_warn_with_limit(
                "FCM payload JSON is not an object (%s); wrapping",
                type(decrypted_json).__name__,
            )
            ret_val = {"_raw_json": decrypted_json}
        else:
            # Not JSON → provide a stable mapping for consumers
            ret_val = (
                {"_raw_text": text}
                if text is not None
                else {"_raw_bytes": decrypted.hex()}
            )

        self._log_verbose(
            "Decrypted data for message %s is: %s", msg.persistent_id, ret_val
        )
        try:
            self.callback(ret_val, msg.persistent_id, self.callback_context)
            self._reset_error_count(ErrorType.NOTIFY)
        except Exception:
            self.logger.exception(
                "Unexpected exception calling notification callback\n"
            )
            self._try_increment_error_count(ErrorType.NOTIFY)

    def _new_input_stream_id_available(self) -> bool:
        return self.last_input_stream_id_reported != self.input_stream_id

    def _get_input_stream_id(self) -> int:
        self.last_input_stream_id_reported = self.input_stream_id
        return self.input_stream_id

    async def _handle_ping(self, p: HeartbeatPing) -> None:
        self.logger.debug(
            "Received heartbeat ping, sending ack: Stream ID: %s, Last: %s, Status: %s",
            p.stream_id,
            p.last_stream_id_received,
            p.status,
        )
        req = HeartbeatAck()

        if self._new_input_stream_id_available():
            req.last_stream_id_received = self._get_input_stream_id()

        await self._send_msg(req)

    async def _handle_iq(self, p: IqStanza) -> None:
        if not p.extension:
            self._log_warn_with_limit(
                "Unexpected IqStanza id received with no extension", str(p)
            )
            return
        if p.extension.id not in (12, 13):
            self._log_warn_with_limit(
                "Unexpected extension id received: %s", p.extension.id
            )
            return

    async def _send_selective_ack(self, persistent_id: str) -> None:
        iqs = IqStanza()
        iqs.type = IqStanza.IqType.SET
        iqs.id = ""
        iqs.extension.id = MCS_SELECTIVE_ACK_ID
        sa = SelectiveAck()
        sa.id.extend([persistent_id])
        iqs.extension.data = sa.SerializeToString()
        self.logger.debug("Sending selective ack for message id %s", persistent_id)
        await self._send_msg(iqs)

    async def _send_heartbeat(self) -> None:
        req = HeartbeatPing()

        if self._new_input_stream_id_available():
            req.last_stream_id_received = self._get_input_stream_id()

        await self._send_msg(req)
        self.logger.debug("Sent heartbeat ping")

    def _reset_error_count(self, error_type: ErrorType) -> None:
        self.sequential_error_counters[error_type] = 0

    def _try_increment_error_count(self, error_type: ErrorType) -> bool:
        if error_type not in self.sequential_error_counters:
            self.sequential_error_counters[error_type] = 0

        # NOTE: returns True to keep going, False when we've decided to stop
        self.sequential_error_counters[error_type] += 1

        if (
            self.config.abort_on_sequential_error_count
            and self.sequential_error_counters[error_type]
            >= self.config.abort_on_sequential_error_count
        ):
            self.logger.debug(
                "Stopping push receiver due to %d sequential errors of type %s (FCM connectivity issue)",
                self.sequential_error_counters[error_type],
                error_type,
            )
            self.do_listen = False
            return False
        return True

    async def _handle_message(self, msg: MessageProto) -> None:
        self.last_message_time = time.time()
        self.input_stream_id += 1

        if isinstance(msg, Close):
            self._log_warn_with_limit("Server sent Close message; worker stopping")
            self.do_listen = False
            return

        if isinstance(msg, LoginResponse):
            if str(msg.error):
                self.logger.error("Received login error response: %s", msg)
                if self._try_increment_error_count(ErrorType.LOGIN):
                    self.do_listen = False
            else:
                self.logger.info("Successfully logged in to MCS endpoint")
                self._reset_error_count(ErrorType.LOGIN)
                self.run_state = FcmPushClientRunState.STARTED
                self.persistent_ids = []
                # Refresh activity timestamp
                self.last_message_time = time.time()
            return

        if isinstance(msg, DataMessageStanza):
            self._handle_data_message(msg)
            self.persistent_ids.append(msg.persistent_id)
            # Acking is cheap; keep it to avoid server redelivery
            await self._send_selective_ack(msg.persistent_id)
        elif isinstance(msg, HeartbeatPing):
            await self._handle_ping(msg)
        elif isinstance(msg, HeartbeatAck):
            self.logger.debug("Received heartbeat ack: %s", msg)
        elif isinstance(msg, IqStanza):
            pass
        else:
            self._log_warn_with_limit("Unexpected message type %s.", type(msg).__name__)
        # Reset error count if a read has been successful
        self._reset_error_count(ErrorType.READ)
        self._reset_error_count(ErrorType.CONNECTION)

    @staticmethod
    async def _open_connection(
        host: str, port: int, ssl_context: ssl.SSLContext
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(host=host, port=port, ssl=ssl_context)

    async def _connect(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
            # create_default_context() blocks the event loop
            ssl_context = await loop.run_in_executor(None, ssl.create_default_context)
            self.reader, self.writer = await self._open_connection(
                host=MCS_HOST, port=MCS_PORT, ssl_context=ssl_context
            )
            self.logger.debug("Connected to MCS endpoint (%s,%s)", MCS_HOST, MCS_PORT)
            return True
        except OSError as oex:
            self.logger.error(
                "Could not connected to MCS endpoint (%s,%s): %s",
                MCS_HOST,
                MCS_PORT,
                oex,
            )
            return False

    def _exp_backoff_with_jitter(self, trycount: int) -> float:
        """Exponential backoff: 0.5,1,2,4,... capped at 60s, plus 10–20% jitter."""
        base: float = min(0.5 * (2 ** (trycount - 1)), 60.0)
        jitter = base * float(random.uniform(0.10, 0.20))
        return float(base + jitter)

    async def _connect_with_retry(self) -> bool:
        self.run_state = FcmPushClientRunState.STARTING_CONNECTION

        trycount = 0
        connected = False
        while (
            trycount < self.config.connection_retry_count
            and not connected
            and self.do_listen
        ):
            trycount += 1
            connected = await self._connect()
            if not connected:
                sleep_time = self._exp_backoff_with_jitter(trycount)
                self.logger.info(
                    "Could not connect to MCS Endpoint on try %s, sleeping for %.2f seconds",
                    trycount,
                    sleep_time,
                )
                await asyncio.sleep(sleep_time)
        if not connected:
            self.logger.error(
                "Unable to connect to MCS endpoint after %s tries", trycount
            )
        return connected

    async def _listen(self) -> None:
        """Listens for push notifications until an error or stop() occurs."""
        try:
            if not await self._connect_with_retry():
                self.run_state = FcmPushClientRunState.STOPPED
                self.do_listen = False
                return

            try:
                await self._login()
            except Exception as ex:
                self.logger.info("Login failed: %s", ex)
                self.do_listen = False
                return

            while self.do_listen:
                try:
                    if msg := await self._receive_msg():
                        await self._handle_message(msg)

                except (ConnectionError, ssl.SSLError) as cex:
                    # Treat stream end/TLS quirks as normal stop; supervisor will restart
                    self.logger.info("FCM stream ended (%s); worker stopping.", cex)
                    self.do_listen = False
                    break

                except (OSError, EOFError, asyncio.IncompleteReadError) as osex:
                    # Normal network life-cycle: log and stop; supervisor will restart
                    self.logger.info("FCM read ended (%s); worker stopping.", osex)
                    self.do_listen = False
                    break

        except asyncio.CancelledError:
            self.logger.debug("Listener task cancelled")
            raise
        except ConnectionResetError:
            # Log a clean warning without noisy traceback; supervisor will reconnect.
            self.logger.warning(
                "FCM connection was reset by the server (this is normal). The connection will be re-established automatically."
            )
            self.do_listen = False
        except Exception as ex:
            self.logger.error(
                "Unknown error in listener: %s\n%s",
                ex,
                traceback.format_exc(),
            )
        finally:
            self.run_state = FcmPushClientRunState.STOPPING
            await self._do_writer_close()
            self.run_state = FcmPushClientRunState.STOPPED

    async def checkin_or_register(self) -> str:
        """Check in if you have credentials otherwise register as a new client.

        :return: The FCM token which is used to identify you with the push end
            point application.
        """
        self.register = FcmRegister(
            self.fcm_config,
            self.credentials,
            self.credentials_updated_callback,
            http_client_session=self._http_client_session,
        )

        try:
            credentials = await self.register.checkin_or_register()
            self.credentials = credentials

            fcm_section = credentials.get("fcm")
            registration_token: str | None = None
            if isinstance(fcm_section, Mapping):
                registration_section = fcm_section.get("registration")
                if isinstance(registration_section, Mapping):
                    token_candidate = registration_section.get("token")
                    if isinstance(token_candidate, str):
                        registration_token = token_candidate

            if registration_token is None:
                raise RuntimeError("FCM registration token missing from credentials.")

            return registration_token
        finally:
            register = self.register
            if register is not None:
                await register.close()
                self.register = None

    async def _start_heartbeat_sender(self) -> None:
        """Send client heartbeats at a fixed interval while started."""
        interval = self.config.client_heartbeat_interval or 0
        if interval <= 0:
            return
        try:
            while self.do_listen:
                await asyncio.sleep(interval)
                if self.run_state == FcmPushClientRunState.STARTED and self.do_listen:
                    self.logger.debug("Sending scheduled client heartbeat")
                    try:
                        await self._send_heartbeat()
                    except Exception as ex:
                        self.logger.debug("Error while sending heartbeat: %s", ex)
        except asyncio.CancelledError:
            self.logger.debug("Heartbeat task cancelled")
            raise

    async def _cancel_and_gather(self) -> None:
        """Cancel all running internal tasks and await their completion."""
        if not self.tasks:
            return
        for t in self.tasks:
            if t and not t.done():
                t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def start(self) -> None:
        """Connect to FCM and start listening for push notifications."""
        self.do_listen = True
        self.run_state = FcmPushClientRunState.STARTING_TASKS
        try:
            # Initialize activity clock to "now" so potential monitors (if any) don't reset
            self.last_message_time = time.time()
            self.tasks = [
                asyncio.create_task(self._listen(), name="googlefindmy.fcm_listen"),
                asyncio.create_task(
                    self._start_heartbeat_sender(), name="googlefindmy.fcm_heartbeat"
                ),
            ]
        except Exception as ex:
            self.logger.error("Unexpected error running FcmPushClient: %s", ex)

    async def stop(self) -> None:
        """Graceful stop: close writer, cancel tasks, mark stopped"""
        if (
            self.stopping_lock
            and self.stopping_lock.locked()
            or self.run_state
            in (
                FcmPushClientRunState.STOPPING,
                FcmPushClientRunState.STOPPED,
            )
        ):
            return

        async with self.stopping_lock:
            try:
                self.run_state = FcmPushClientRunState.STOPPING
                self.do_listen = False
                await self._do_writer_close()
                await self._cancel_and_gather()
            finally:
                self.run_state = FcmPushClientRunState.STOPPED

    def is_started(self) -> bool:
        return self.run_state == FcmPushClientRunState.STARTED

    async def send_message(self, raw_data: bytes, persistent_id: str) -> None:
        """Not implemented, does nothing atm."""
        dms = DataMessageStanza()
        dms.persistent_id = persistent_id
        # Not supported yet
