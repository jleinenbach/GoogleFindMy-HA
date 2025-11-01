# tests/test_no_micro_sign_literal.py
"""Ensure the deprecated micro sign literal is absent from source assets."""

from __future__ import annotations

from pathlib import Path

FORBIDDEN_MICRO_SIGN = "\u00b5"
_TARGET_SUFFIXES = {".py", ".json"}


def test_no_micro_sign_in_repository() -> None:
    """Fail when the deprecated micro sign appears in tracked source files."""

    repo_root = Path(__file__).resolve().parent.parent
    offenders: list[Path] = []

    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue

        if path.suffix.lower() not in _TARGET_SUFFIXES:
            continue

        contents = path.read_text(encoding="utf-8")
        if FORBIDDEN_MICRO_SIGN in contents:
            offenders.append(path.relative_to(repo_root))

    assert not offenders, (
        "Use the Greek small letter mu (\\u03bc) instead of the legacy micro sign (\\u00b5). "
        f"Found in: {', '.join(str(path) for path in offenders)}"
    )
