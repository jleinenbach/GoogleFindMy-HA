# custom_components/googlefindmy/Auth/log_safety.py
"""Exception summaries for log records that must not carry foreign text.

The Auth contract (``Auth/AGENTS.md``, logging section) keeps raw exception
text out of the message: a producer such as ``gpsoauth``, ``requests`` or
``aiohttp`` may echo a token, an account identifier or a response body in
``str(exc)``, and a broad ``except Exception`` handler forwards whatever it
caught. :func:`describe_exception` reduces an exception to what is safe to
print: its type, a structured ``error_kind`` or ``errno`` when present, and
otherwise the length of its text. Exceptions raised by this package keep
their message, because those messages are written under the same contract;
the payload guard (shape (H)) scans their constructors for payload names,
not for a foreign exception interpolated into the text, so an own
exception built as ``Own(f"... {err}")`` from a foreign ``err`` would pass
its text through (no such site under ``Auth/`` on 2026-09-17).
:func:`exception_origin` names the innermost frame of a traceback without
its text, for the records that used to append ``traceback.format_exc()``.
"""

from __future__ import annotations

import traceback
from pathlib import PurePath

_OWN_PACKAGE = "custom_components.googlefindmy"
_TEXT_LIMIT = 200


def describe_exception(exc: BaseException, *, limit: int = _TEXT_LIMIT) -> str:
    """Return a log-safe one-line summary of ``exc``.

    Order of preference: ``error_kind`` (set by the gpsoauth classifiers),
    ``errno`` for :class:`OSError`, the clipped message for exceptions of
    this package, the bare type name for an empty message, and otherwise
    the type name with the character count of the text that is withheld.
    ``<Name> (unprintable)`` when ``__str__`` or a metadata property
    (``error_kind``, ``errno``, the class name or module) raises, and
    ``<unnamed>`` for a class whose name is not a plain ``str``: the helper
    runs inside catch-all handlers and never raises itself, whatever the
    producer raises, ``BaseException`` subclasses included. ``errno`` is
    printed only when it is an ``int``. Text from ``__str__`` is normalised
    to an exact ``str`` before it is measured or rendered, so a ``str``
    subclass cannot fail or lie during the formatting that follows.
    """
    # Total by construction: this helper runs inside catch-all handlers, so
    # a producer whose metadata property, ``__str__``, ``__getattribute__``
    # or metaclass raises must not escape as a second exception from the
    # handler. ``BaseException`` on purpose: a ``KeyboardInterrupt`` or
    # ``SystemExit`` raised from an exception object's own attribute read
    # is the producer's doing, not the user's (under Home Assistant SIGINT is
    # loop-bound; in the CLI a Ctrl-C landing inside these few reads is
    # absorbed once, the next one is not); nothing here awaits, so a task
    # cancellation cannot surface inside these reads.
    try:
        name = type(exc).__name__
        if type(name) is not str:  # a metaclass may hand back text of its own
            name = "<unnamed>"
    except BaseException:  # noqa: BLE001 - a metaclass may fail on __name__
        name = "<unnamed>"
    try:
        kind = getattr(exc, "error_kind", None)
        if isinstance(kind, str) and kind:
            return f"{name} (kind={kind})"
        if isinstance(exc, OSError) and isinstance(exc.errno, int):
            return f"{name} (errno={exc.errno})"
        # ``str()`` returns what ``__str__`` returns, a ``str`` subclass
        # included, so its length, its formatting and its slicing are the
        # producer's code too. ``str.__str__`` hands back the base class's
        # value, so everything after this ``try`` runs on an exact ``str``:
        # the rendering below cannot raise a second failure, and the size
        # that decides about clipping is measured on the exact value.
        text = str.__str__(str(exc))
        size = len(text)
        own = bool(type(exc).__module__.startswith(_OWN_PACKAGE))
    except BaseException:  # noqa: BLE001 - a producer's property or __str__ may fail
        return f"{name} (unprintable)"
    if own:
        return f"{name}: {text}" if size <= limit else f"{name}: {text[: limit - 1]}…"
    if size == 0:
        return name
    return f"{name} ({size} chars withheld)"


def exception_origin(exc: BaseException) -> str:
    """Return ``file:line in function`` of the innermost frame, without text.

    Replaces ``traceback.format_exc()`` in records that must not carry the
    producer's message: the location is kept, the text is withheld.
    """
    # ``lookup_lines=False``: only file, line and name are used, and the
    # default would read each frame's source line through ``linecache``
    # (disk I/O on the event loop for a diagnostic string). Total like
    # :func:`describe_exception`: a producer's ``__getattribute__`` may
    # fail on ``__traceback__``, and a foreign traceback object may fail
    # while it is walked; ``BaseException`` for the same reason as there.
    try:
        frames = traceback.StackSummary.extract(
            traceback.walk_tb(exc.__traceback__), lookup_lines=False
        )
        if not frames:
            return "no traceback"
        frame = frames[-1]
        return f"{PurePath(frame.filename).name}:{frame.lineno} in {frame.name}"
    except BaseException:  # noqa: BLE001 - the traceback is the producer's too
        return "no traceback (unprintable)"
