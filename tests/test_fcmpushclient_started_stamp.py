# tests/test_fcmpushclient_started_stamp.py
"""Login-success path stamps the per-client STARTED-transition timestamp.

Covers the ``LoginResponse`` success branch of ``FcmPushClient._handle_message``
(the ``_started_monotonic`` stamp added alongside ``run_state = STARTED`` and the
``persistent_ids`` reset). The supervisor's first-locate reconnect measures
"session age since STARTED" from this field instead of the pre-start supervisor
timestamp, so the stamp must be set exactly when the client reaches STARTED.

Additive-composition discipline (same as ``FcmMessageSlim`` / ``FcmHandleSlim``):
the real unbound async ``_handle_message`` runs against a light container that
mirrors only the attributes the login-success branch touches, without the heavy
production constructor. A real ``LoginResponse()`` is used (not a namespace) so
the production ``isinstance(msg, LoginResponse)`` dispatch is exercised for real.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    ErrorType,
    FcmPushClientRunState,
    LoginResponse,
)
from tests.helpers.fcm_handle_stub import FcmMessageSlim


@pytest.mark.asyncio
async def test_login_success_stamps_started_monotonic() -> None:
    """A successful login sets ``_started_monotonic``, STARTED and resets ids."""
    slim = FcmMessageSlim()
    # Attributes the login-success branch reads/writes beyond the Close preamble.
    slim._reset_error_count = Mock()  # type: ignore[attr-defined]
    slim.run_state = FcmPushClientRunState.CREATED  # type: ignore[attr-defined]
    slim.persistent_ids = ["stale-pid"]  # type: ignore[attr-defined]
    slim._first_data_message_delivered = True  # type: ignore[attr-defined]
    slim._started_monotonic = None  # type: ignore[attr-defined]

    await slim._handle_message(LoginResponse())  # type: ignore[arg-type]

    # The STARTED-transition stamp is set (was None) -> a real float timestamp.
    assert isinstance(slim._started_monotonic, float)  # type: ignore[attr-defined]
    # And the accompanying STARTED state + persistent-ids reset happened.
    assert slim.run_state == FcmPushClientRunState.STARTED  # type: ignore[attr-defined]
    assert slim.persistent_ids == []  # type: ignore[attr-defined]
    # The separate delivery proof is reset too, so the fresh session correctly
    # reads as "not yet delivered" until a real data message arrives.
    assert slim._first_data_message_delivered is False  # type: ignore[attr-defined]
    slim._reset_error_count.assert_called_once_with(ErrorType.LOGIN)  # type: ignore[attr-defined]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
