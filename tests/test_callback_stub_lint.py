"""Lint test preventing inline Home Assistant callback stubs."""

from __future__ import annotations

from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
PATTERN = "lambda func: func"


@pytest.mark.parametrize(
    "path",
    sorted(
        (
            candidate
            for candidate in TESTS_ROOT.glob("**/*.py")
            if "__pycache__" not in candidate.parts
        ),
        key=lambda item: item.as_posix(),
    ),
)
def test_no_lambda_identity_callback_stub(path: Path) -> None:
    """Ensure tests rely on the shared helper instead of inline lambda stubs."""

    if path.resolve() == Path(__file__).resolve():
        return

    contents = path.read_text(encoding="utf-8")
    if PATTERN in contents:
        relative_path = path.relative_to(TESTS_ROOT)
        pytest.fail(
            "`lambda func: func` detected in"
            f" {relative_path}. Use"
            " `tests.helpers.install_homeassistant_core_callback_stub` instead."
        )
