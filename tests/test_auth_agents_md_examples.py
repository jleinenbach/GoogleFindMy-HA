# tests/test_auth_agents_md_examples.py
"""The logging examples in Auth/AGENTS.md must obey the rule they illustrate.

The contract forbids a traceback and a bare producer exception in Auth
records; its own ``python`` code blocks are what contributors copy. This test
extracts those blocks, wraps each in a foreign ``except`` handler and runs the
payload guard's scanner on it, so a block that shows ``exc_info=err`` under
the rule that forbids it fails the suite (Codex round 16 on PR #1304).
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

from custom_components import googlefindmy
from tests.test_guard_logging_payloads import _scan_tree

_CONTRACT = Path(googlefindmy.__file__).resolve().parent / "Auth" / "AGENTS.md"
_BLOCK = re.compile(r"```python\n(.*?)```", re.S)


def _blocks() -> list[str]:
    return _BLOCK.findall(_CONTRACT.read_text(encoding="utf-8"))


def test_contract_has_python_examples() -> None:
    """The extraction is not a vacuum: the contract carries logging examples."""
    assert len(_blocks()) >= 2


def test_logging_examples_pass_the_guard_as_foreign_handler_bodies() -> None:
    """Each block, placed inside ``except Exception as err``, reports nothing."""
    for index, block in enumerate(_blocks()):
        wrapped = (
            "def example():\n    try:\n        pass\n    except Exception as err:\n"
            + textwrap.indent(block, " " * 8)
        )
        offenders, _ = _scan_tree(ast.parse(wrapped), "Auth/AGENTS.md")
        assert offenders == [], (index, offenders)
