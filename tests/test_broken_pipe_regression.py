"""Regression test for the 'Broken Pipe' shared session bug."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.googlefindmy.api import GoogleFindMyAPI


@pytest.mark.asyncio
async def test_api_must_not_close_shared_session() -> None:
    """Ensure the API releases the session reference WITHOUT closing the connection.

    Closing the shared HA session causes [Errno 32] Broken Pipe for all other
    integrations. This test ensures we never revert to that behavior.
    """

    # 1. Setup Mock Session
    mock_session = MagicMock()
    mock_session.close = AsyncMock()
    mock_session.closed = False

    # 2. Init API
    api = GoogleFindMyAPI(mock_session)

    # 3. Trigger Cleanup
    await api.close()

    # 4. LOUD ASSERTION
    if mock_session.close.called:
        pytest.fail(
            "CRITICAL REGRESSION: GoogleFindMyAPI attempted to close the shared session!\n"
            "-----------------------------------------------------------------------\n"
            "Current Behavior: 'await session.close()' was called.\n"
            "Consequence: This crashes Home Assistant networking (Broken Pipe).\n"
            "Fix: In 'api.py -> close()', remove the call to session.close().\n"
            "-----------------------------------------------------------------------"
        )

    # 5. Verify Reference Clearing
    # Accessing private attribute strictly for test verification
    assert api._session is None, "API should clear its local session reference."
