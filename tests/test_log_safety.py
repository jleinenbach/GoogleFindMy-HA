# tests/test_log_safety.py
"""Tests for the log-safe exception summary used across the Auth package."""

from __future__ import annotations

import errno

from custom_components.googlefindmy.Auth.log_safety import (
    describe_exception,
    exception_origin,
)
from custom_components.googlefindmy.NovaApi.nova_request import NovaHTTPError

_ECHO = "Rejected token ya29.a0AfB_LEAKED for user@example.com"


def test_foreign_exception_is_reduced_to_type_and_length() -> None:
    """A producer's text is withheld; only its type and size are printed."""
    summary = describe_exception(RuntimeError(_ECHO))
    assert summary == f"RuntimeError ({len(_ECHO)} chars withheld)"
    assert "ya29." not in summary
    assert "example.com" not in summary


def test_error_kind_wins_over_text() -> None:
    """A classified exception prints its kind, never its text."""
    err = RuntimeError(_ECHO)
    err.error_kind = "badauthentication"  # type: ignore[attr-defined]
    assert describe_exception(err) == "RuntimeError (kind=badauthentication)"


def test_oserror_prints_errno_not_text() -> None:
    """`requests` transport errors are OSErrors; errno is a code, the text is not."""
    err = ConnectionRefusedError(errno.ECONNREFUSED, f"refused {_ECHO}")
    summary = describe_exception(err)
    assert summary == f"ConnectionRefusedError (errno={errno.ECONNREFUSED})"
    assert "ya29." not in summary


def test_empty_message_is_the_bare_type() -> None:
    """`asyncio.TimeoutError()` has no text; the type alone is the record."""
    assert describe_exception(TimeoutError()) == "TimeoutError"


def test_own_exception_keeps_its_contract_bound_message() -> None:
    """Exceptions of this package keep their text (guard shape (H) scans it)."""
    err = NovaHTTPError(503, "from Nova")
    summary = describe_exception(err)
    assert summary == "NovaHTTPError: HTTP Server Error 503: from Nova"


def test_own_exception_text_is_clipped() -> None:
    """A long own message is clipped with an ellipsis at the limit."""
    err = NovaHTTPError(500, "x" * 300)
    summary = describe_exception(err, limit=20)
    assert summary == "NovaHTTPError: HTTP Server Error 5\u2026"


def test_exception_origin_names_the_frame_without_text() -> None:
    """The innermost frame is printed as ``file:line in function``, no message."""

    def _producer() -> None:
        raise RuntimeError(_ECHO)

    try:
        _producer()
    except RuntimeError as err:
        origin = exception_origin(err)
    assert origin.startswith("test_log_safety.py:")
    assert origin.endswith(" in _producer")
    assert "ya29." not in origin


def test_exception_origin_without_traceback() -> None:
    """An exception that was never raised has no frame to name."""
    assert exception_origin(RuntimeError(_ECHO)) == "no traceback"


def test_unprintable_exception_is_named_not_rendered() -> None:
    """A producer whose ``__str__`` raises is summarised as unprintable."""

    class _Broken(Exception):
        def __str__(self) -> str:
            raise ValueError("no text")

    _Broken.__module__ = "gpsoauth.exceptions"
    assert describe_exception(_Broken()) == "_Broken (unprintable)"
