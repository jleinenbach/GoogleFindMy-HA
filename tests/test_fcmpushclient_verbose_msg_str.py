# tests/test_fcmpushclient_verbose_msg_str.py
"""``_msg_str`` describes an MCS message by type and field names, never values.

Verbose mode used to pretty-print the whole message as JSON: for a
``DataMessageStanza`` that is the push payload, the persistent id and the
token; for a ``LoginResponse`` the server-side ids. `AGENTS.md` section 5
forbids raw payloads and tokens in log records at every level, verbose or
not.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    FcmPushClient,
)
from custom_components.googlefindmy.Auth.firebase_messaging.proto.mcs_pb2 import (
    DataMessageStanza,
)


def _stanza() -> DataMessageStanza:
    msg = DataMessageStanza()
    msg.id = "msg-7f3a9c"
    # `from` is a keyword, hence setattr.
    setattr(msg, "from", "sender-41b2e8d0")
    msg.category = "com.google.android.apps.adm"
    msg.token = "dq9x3EtH2kY:APA91bF0VzWc8ghUGrOpN1JmQ5aTe4bRxL7sKdZyCvIiHpMuWn"
    msg.persistent_id = "0:1758100000000000%9e4b2a1cf9fd7ecd"
    item = msg.app_data.add()
    item.key = "com.google.android.apps.adm.FCM_PAYLOAD"
    item.value = "CgtwYXlsb2FkLWJ5dGVzLW5vdC1mb3ItbG9ncw=="
    return msg


def _msg_str(verbose: bool) -> str:
    stand_in = SimpleNamespace(config=SimpleNamespace(log_debug_verbose=verbose))
    return FcmPushClient._msg_str(stand_in, _stanza())  # type: ignore[arg-type]


def test_verbose_msg_str_names_fields_not_values() -> None:
    text = _msg_str(verbose=True)
    assert text.startswith("DataMessageStanza fields=")
    for name in ("id", "from", "category", "token", "app_data", "persistent_id"):
        assert name in text
    stanza = _stanza()
    for value in (
        stanza.token,
        stanza.persistent_id,
        stanza.id,
        getattr(stanza, "from"),
        stanza.app_data[0].value,
    ):
        assert value not in text
        assert all(value[i : i + 8] not in text for i in range(0, len(value) - 7)), (
            f"a window of {value!r} reached the verbose message string"
        )


def test_non_verbose_msg_str_is_the_type_name() -> None:
    assert _msg_str(verbose=False) == "DataMessageStanza"
