# tests/test_log_safety.py
"""Tests for the log-safe exception summary used across the Auth package."""

from __future__ import annotations

import errno

from custom_components.googlefindmy.Auth.log_safety import (
    describe_exception,
    exception_origin,
)
from custom_components.googlefindmy.NovaApi.nova_request import NovaHTTPError

_ECHO = "Rejected token ECHOED-TOKEN-4f8xLEAK for user@example.com"


def test_foreign_exception_is_reduced_to_type_and_length() -> None:
    """A producer's text is withheld; only its type and size are printed."""
    summary = describe_exception(RuntimeError(_ECHO))
    assert summary == f"RuntimeError ({len(_ECHO)} chars withheld)"
    assert "4f8xLEAK" not in summary
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
    assert "4f8xLEAK" not in summary


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
    assert "4f8xLEAK" not in origin


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


def test_raising_metadata_property_is_named_not_raised() -> None:
    """A producer whose ``error_kind`` or ``errno`` property raises is contained.

    The helper runs inside catch-all handlers (``_handle_notification_async``
    and its siblings); a second exception from the summary would escape the
    handler that was meant to contain the first one.
    """

    class _Hostile(Exception):
        @property
        def error_kind(self) -> str:
            raise RuntimeError("no kind")

    class _HostileOS(OSError):
        @property
        def errno(self) -> int:  # type: ignore[override]
            raise RuntimeError("no errno")

    _Hostile.__module__ = "gpsoauth.exceptions"
    _HostileOS.__module__ = "aiohttp.client_exceptions"
    assert describe_exception(_Hostile("text")) == "_Hostile (unprintable)"
    assert describe_exception(_HostileOS("text")) == "_HostileOS (unprintable)"


def test_base_exception_from_a_producer_property_is_contained() -> None:
    """A metadata property or ``__str__`` raising outside ``Exception`` is contained too.

    ``except Exception`` left ``KeyboardInterrupt`` and ``SystemExit`` from
    a producer's attribute read free to escape the catch-all logging paths
    (Codex review on 714596f); the helper promises never to raise itself.
    """

    class _Interrupting(Exception):
        @property
        def error_kind(self) -> str:
            raise KeyboardInterrupt

    class _Exiting(Exception):
        def __str__(self) -> str:
            raise SystemExit(3)

    class _NamelessMeta(type):
        @property
        def __name__(cls) -> str:  # type: ignore[override]
            raise RuntimeError("no name")

    class _Nameless(Exception, metaclass=_NamelessMeta):
        pass

    _Interrupting.__module__ = "gpsoauth.exceptions"
    _Exiting.__module__ = "aiohttp.client_exceptions"
    assert describe_exception(_Interrupting("text")) == "_Interrupting (unprintable)"
    assert describe_exception(_Exiting("text")) == "_Exiting (unprintable)"
    assert describe_exception(_Nameless("text")) == "<unnamed> (4 chars withheld)"


def test_exception_origin_survives_a_hostile_traceback_read() -> None:
    """``__traceback__`` read through a raising ``__getattribute__`` yields no frame, no raise."""

    class _Opaque(Exception):
        def __getattribute__(self, name: str) -> object:
            if name == "__traceback__":
                raise KeyboardInterrupt
            return super().__getattribute__(name)

    assert exception_origin(_Opaque("text")) == "no traceback (unprintable)"


def test_text_subclass_and_string_errno_are_contained() -> None:
    """A ``str`` subclass from ``__str__`` and a non-int ``errno`` stay out of the record."""

    class _Text(str):
        __slots__ = ()

        def __len__(self) -> int:
            raise RuntimeError("no length")

    class _Sized(Exception):
        def __str__(self) -> str:
            return _Text("abc")

    class _NamedMeta(type):
        @property
        def __name__(cls) -> str:  # type: ignore[override]
            return _Text("LEAKED-NAME")  # not a plain str: withheld

    class _Named(Exception, metaclass=_NamedMeta):
        pass

    _Sized.__module__ = "gpsoauth.exceptions"
    # The length is measured on the exact ``str``, not on the subclass that
    # refuses to be measured, so the size is printed instead of ``unprintable``.
    # ``_Text.__len__`` is therefore unreachable from here since the text is
    # normalised; it stays as the guard that would fire if that normalisation
    # were removed, and the lying counterpart is pinned in the test below.
    assert describe_exception(_Sized()) == "_Sized (3 chars withheld)"
    assert (
        describe_exception(OSError("TOKEN-LEAK", "x")) == "OSError (20 chars withheld)"
    )
    assert describe_exception(_Named("text")).startswith("<unnamed> (")


def test_own_exception_text_is_rendered_as_an_exact_str() -> None:
    """A ``str`` subclass from ``__str__`` cannot fail or lie while rendering."""

    class _Unformattable(str):
        __slots__ = ()

        def __format__(self, format_spec: str, /) -> str:
            raise RuntimeError("no format")

    class _Unsliceable(str):
        __slots__ = ()

        def __getitem__(self, key: object, /) -> str:
            raise RuntimeError("no slice")

    class _Lying(str):
        __slots__ = ()

        def __len__(self) -> int:
            return 300  # three characters claiming to be over the limit

    class _Own(Exception):
        def __str__(self) -> str:
            return _Unformattable("abc")

    class _OwnLong(Exception):
        def __str__(self) -> str:
            return _Unsliceable("x" * 250)

    class _OwnLying(Exception):
        def __str__(self) -> str:
            return _Lying("abc")

    class _Foreign(Exception):
        def __str__(self) -> str:
            return _Unformattable("abcd")

    for own in (_Own, _OwnLong, _OwnLying):
        own.__module__ = "custom_components.googlefindmy.Auth.producer"
    _Foreign.__module__ = "gpsoauth.exceptions"

    # Rendering, clipping and measuring all run on the exact ``str``.
    assert describe_exception(_Own()) == "_Own: abc"
    assert describe_exception(_OwnLong()) == "_OwnLong: " + "x" * 199 + "\u2026"
    assert describe_exception(_OwnLying()) == "_OwnLying: abc"
    # Control: a foreign producer keeps its text withheld, at its true size.
    assert describe_exception(_Foreign()) == "_Foreign (4 chars withheld)"

    class _ForeignLying(Exception):
        def __str__(self) -> str:
            return _Lying("abc")

    _ForeignLying.__module__ = "gpsoauth.exceptions"
    # The withheld size is the true one: the count comes from the exact
    # ``str``, not from the subclass that claims to be over the limit.
    assert describe_exception(_ForeignLying()) == "_ForeignLying (3 chars withheld)"


def test_producer_metadata_is_normalised_before_rendering() -> None:
    """``error_kind``, ``errno`` and ``__module__`` are exact before they are used."""

    # The hostile dunders below are unreachable while the normalisation holds:
    # each is the detector for exactly one mutant, not dead code. The same
    # holds for ``_Text.__len__`` in the containment test above.

    class _Kind(str):
        __slots__ = ()

        def __format__(self, format_spec: str, /) -> str:
            return "KIND-LEAK"

    class _Errno(int):
        __slots__ = ()

        def __format__(self, format_spec: str, /) -> str:
            return "ERRNO-LEAK"

    class _Module(str):
        __slots__ = ()

        def startswith(  # type: ignore[override]
            self, prefix: object, /, *args: object, **kwargs: object
        ) -> bool:
            return True

    class _Kinded(Exception):
        error_kind = _Kind("bad_auth")

    class _LongKinded(Exception):
        error_kind = "k" * 250

    class _Errnoed(OSError):
        pass

    class _Foreign(Exception):
        def __str__(self) -> str:
            return "TOKEN-LEAK for user@example.com"

    _Kinded.__module__ = "gpsoauth.exceptions"
    _LongKinded.__module__ = "gpsoauth.exceptions"
    _Foreign.__module__ = _Module("gpsoauth.exceptions")

    # The kind is rendered from the base class's value, not from __format__.
    assert describe_exception(_Kinded()) == "_Kinded (kind=bad_auth)"
    # An over-long kind is clipped like the message is, at the same limit.
    assert (
        describe_exception(_LongKinded())
        == "_LongKinded (kind=" + "k" * 199 + "\u2026)"
    )
    # An ``errno`` subclass is printed by value, not by its own formatting.
    errnoed = _Errnoed("x")
    errnoed.errno = _Errno(13)
    assert describe_exception(errnoed) == "_Errnoed (errno=13)"
    # A module name that lies about its prefix does not release the text.
    assert describe_exception(_Foreign()) == "_Foreign (31 chars withheld)"


def test_a_neighbouring_package_is_not_this_package() -> None:
    """Ownership needs the exact name or a dotted child, not a shared prefix."""

    class _Fork(Exception):
        def __str__(self) -> str:
            return "SECRET-BODY"

    class _Own(Exception):
        def __str__(self) -> str:
            return "own text"

    _Fork.__module__ = "custom_components.googlefindmy_fork.evil"
    _Own.__module__ = "custom_components.googlefindmy"

    assert describe_exception(_Fork()) == "_Fork (11 chars withheld)"
    assert describe_exception(_Own()) == "_Own: own text"


def test_a_limit_that_cannot_carry_the_ellipsis_yields_no_text() -> None:
    """Below one character there is nothing to show, not a slice from the end."""

    class _Own(Exception):
        def __str__(self) -> str:
            return "SECRET-BODY"

    class _Kinded(Exception):
        error_kind = "bad_authentication"

    _Own.__module__ = "custom_components.googlefindmy.Auth.producer"
    _Kinded.__module__ = "gpsoauth.exceptions"

    # ``limit - 1`` would be a negative index here and would cut from the end,
    # handing out the tail of the producer's text under a limit of zero.
    assert describe_exception(_Own(), limit=0) == "_Own: "
    assert describe_exception(_Own(), limit=1) == "_Own: \u2026"
    assert describe_exception(_Kinded(), limit=0) == "_Kinded (kind=)"
    assert describe_exception(_Kinded(), limit=1) == "_Kinded (kind=\u2026)"

    class _Exact(Exception):
        def __str__(self) -> str:
            return "x" * 20

    _Exact.__module__ = "custom_components.googlefindmy.Auth.producer"
    # A text of exactly ``limit`` characters is still shown in full: the
    # ellipsis costs a character, so clipping it would shorten it below limit.
    assert describe_exception(_Exact(), limit=20) == "_Exact: " + "x" * 20
