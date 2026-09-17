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
    """
    name = type(exc).__name__
    kind = getattr(exc, "error_kind", None)
    if isinstance(kind, str) and kind:
        return f"{name} (kind={kind})"
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"{name} (errno={exc.errno})"
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 - a producer's __str__ may itself fail
        return f"{name} (unprintable)"
    if type(exc).__module__.startswith(_OWN_PACKAGE):
        return (
            f"{name}: {text}" if len(text) <= limit else f"{name}: {text[: limit - 1]}…"
        )
    if not text:
        return name
    return f"{name} ({len(text)} chars withheld)"


def exception_origin(exc: BaseException) -> str:
    """Return ``file:line in function`` of the innermost frame, without text.

    Replaces ``traceback.format_exc()`` in records that must not carry the
    producer's message: the location is kept, the text is withheld.
    """
    # ``lookup_lines=False``: only file, line and name are used, and the
    # default would read each frame's source line through ``linecache``
    # (disk I/O on the event loop for a diagnostic string).
    frames = traceback.StackSummary.extract(
        traceback.walk_tb(exc.__traceback__), lookup_lines=False
    )
    if not frames:
        return "no traceback"
    frame = frames[-1]
    return f"{PurePath(frame.filename).name}:{frame.lineno} in {frame.name}"
