# tests/test_foreign_reading_docstring_guard.py
"""Guard for the removal criterion of the provisional P-256 readings (#223).

``P256_FOREIGN_READINGS`` tries several readings because no measured source
settles how a P-256 tracker derives its scalar and nonce. The criterion for
removing the losers lives in one place, the attribute docstring of that
constant, and ``docs/CRYPTOGRAPHY.md`` carries an open-item section that
records field reports. Neither is code, so a refactor can drop either one
without any test noticing; the readings would then stay forever, silently.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import (
    P256_FOREIGN_READINGS,
)

_ROOT = Path(__file__).resolve().parents[1]
_CRYPTOR = (
    _ROOT
    / "custom_components"
    / "googlefindmy"
    / "FMDNCrypto"
    / "foreign_tracker_cryptor.py"
)
_CRYPTOGRAPHY_DOC = _ROOT / "docs" / "CRYPTOGRAPHY.md"
_OPEN_ITEM_HEADING = "## Open item: provisional P-256 readings"


def _attribute_docstring(path: Path, name: str) -> str:
    """Return the string statement that directly follows ``name = ...``.

    Read from the syntax tree, so a comment or a string elsewhere in the
    module that happens to contain the same words does not count.
    """
    body = ast.parse(path.read_text(encoding="utf-8")).body
    for index, node in enumerate(body):
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        following = body[index + 1] if index + 1 < len(body) else None
        if (
            isinstance(following, ast.Expr)
            and isinstance(following.value, ast.Constant)
            and isinstance(following.value.value, str)
        ):
            return following.value.value
        return ""
    raise AssertionError(f"{name} is not assigned at module level in {path}")


def _normalized(text: str) -> str:
    """Collapse every run of whitespace so line wrapping does not matter."""
    return re.sub(r"\s+", " ", text)


class TestRemovalCriterionStaysDocumented:
    """Pins the removal criterion and the open item it refers to.

    Blind spot: the guard checks that the criterion is written down, not that
    anyone applies it. Whether a field report has arrived, and whether the
    table in the open-item section lists it, is outside its reach.

    Retirement: delete this guard once the open-item section is removed from
    ``docs/CRYPTOGRAPHY.md`` and no reading carries ``provisional=True`` any
    more; until both hold, every test here must stay.
    """

    def test_docstring_points_to_the_feedback_url(self) -> None:
        """Users must know where to post the reading that worked."""
        doc = _normalized(_attribute_docstring(_CRYPTOR, "P256_FOREIGN_READINGS"))
        assert re.search(r"``FOREIGN_READING_FEEDBACK_URL``", doc)

    def test_docstring_states_the_removal_criterion(self) -> None:
        """A confirmation makes the other readings removal candidates."""
        doc = _normalized(_attribute_docstring(_CRYPTOR, "P256_FOREIGN_READINGS"))
        assert re.search(
            r"confirms one P-256 reading.*other P-256 readings become removal candidates",
            doc,
        )

    def test_docstring_states_that_a_contradiction_blocks_removal(self) -> None:
        """Two devices reporting different readings need both readings."""
        doc = _normalized(_attribute_docstring(_CRYPTOR, "P256_FOREIGN_READINGS"))
        assert re.search(r"no report contradicts it", doc)
        assert re.search(r"a WARNING does not", doc)

    def test_docstring_binds_a_reading_to_its_variant(self) -> None:
        """A reading an EID variant still derives with cannot go alone."""
        doc = _normalized(_attribute_docstring(_CRYPTOR, "P256_FOREIGN_READINGS"))
        assert re.search(r"removed only together with that variant", doc)

    def test_docstring_refers_to_the_open_item_section(self) -> None:
        """The criterion names where the open item is tracked."""
        doc = _normalized(_attribute_docstring(_CRYPTOR, "P256_FOREIGN_READINGS"))
        assert re.search(
            r"Open item: provisional P-256 readings.{0,20}docs/CRYPTOGRAPHY\.md", doc
        )

    def test_open_item_section_exists_while_a_reading_is_provisional(self) -> None:
        """The open item may only disappear together with the last provisional reading."""
        if not any(reading.provisional for reading in P256_FOREIGN_READINGS):
            pytest.skip("no provisional reading left; see the retirement note")
        lines = _CRYPTOGRAPHY_DOC.read_text(encoding="utf-8").splitlines()
        assert _OPEN_ITEM_HEADING in lines
