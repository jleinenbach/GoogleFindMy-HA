# tests/test_precommit_update_doctoc.py
"""Unit tests for the in-repo DocToc refresh pre-commit hook."""

from __future__ import annotations

from pathlib import Path

import pytest

from script.precommit_hooks import update_doctoc


def _titles(document: str) -> list[str]:
    """Return the heading titles the hook would put into the table of contents."""

    return [
        title for _level, title in update_doctoc._iter_headings(document.splitlines())
    ]


def test_headings_outside_code_are_collected() -> None:
    """Plain headings keep their level and document order."""

    document = "# Top\n\ntext\n\n## Second\n\n### Third\n"

    assert update_doctoc._iter_headings(document.splitlines()) == [
        (1, "Top"),
        (2, "Second"),
        (3, "Third"),
    ]


def test_comment_inside_backtick_fence_is_not_a_heading() -> None:
    """A shell comment inside a fenced block must not become a heading."""

    document = (
        "# Real\n\n```bash\n# 1. Edit pyproject.toml\npoetry lock\n```\n\n## After\n"
    )

    assert _titles(document) == ["Real", "After"]


def test_comment_inside_tilde_fence_is_not_a_heading() -> None:
    """Tilde fences delimit code blocks just like backtick fences."""

    document = "# Real\n\n~~~bash\n# Not a heading\n~~~\n\n## After\n"

    assert _titles(document) == ["Real", "After"]


def test_shorter_fence_does_not_close_a_longer_one() -> None:
    """A closing fence must be at least as long as the opening fence."""

    document = "````text\n```\n# Still code\n````\n\n# After\n"

    assert _titles(document) == ["After"]


def test_fence_of_a_different_character_does_not_close() -> None:
    """A tilde run does not close a backtick fence."""

    document = "```text\n~~~\n# Still code\n```\n\n# After\n"

    assert _titles(document) == ["After"]


def test_closing_fence_must_not_carry_an_info_string() -> None:
    """Only the opening fence may carry an info string."""

    document = "```text\n```python\n# Still code\n```\n\n# After\n"

    assert _titles(document) == ["After"]


def test_indented_fence_up_to_three_spaces_opens_a_block() -> None:
    """Up to three leading spaces still open a fenced block."""

    document = "# Real\n\n   ```bash\n# Not a heading\n   ```\n\n## After\n"

    assert _titles(document) == ["Real", "After"]


def test_unclosed_fence_raises_instead_of_dropping_headings() -> None:
    """An unterminated fence is reported rather than hiding later headings."""

    document = "# Real\n\ntext\n\n```bash\n# Not a heading\n\n## Also not a heading\n"

    with pytest.raises(
        update_doctoc.TocGenerationError, match="line 5 is never closed"
    ):
        _titles(document)


def test_closing_fence_may_carry_trailing_spaces() -> None:
    """Trailing whitespace after a closing fence is not an info string."""

    document = "```\n# Not a heading\n```   \n# After\n"

    assert _titles(document) == ["After"]


def test_longer_fence_closes_a_shorter_one() -> None:
    """A closing fence may be longer than the opening fence."""

    document = "```\n# Not a heading\n````\n# After\n"

    assert _titles(document) == ["After"]


def test_two_backticks_do_not_open_a_fence() -> None:
    """A fence needs at least three backticks."""

    assert _titles("``\n# Heading\n") == ["Heading"]


def test_fence_indented_four_spaces_does_not_open_a_block() -> None:
    """Four leading spaces make an indented code line, not a fence."""

    assert _titles("    ```\n# Heading\n") == ["Heading"]


def test_backtick_in_info_string_does_not_open_a_fence() -> None:
    """A backtick fence's info string may not contain a backtick."""

    assert _titles("```a`b\n# Heading\n") == ["Heading"]


def test_list_item_fence_closes_with_deeper_indentation() -> None:
    """A fence inside a list item may close with up to three more spaces."""

    document = "- item\n\n  ```bash\n  x\n     ```\n\n# After\n\n## Later\n"

    assert _titles(document) == ["After", "Later"]


def test_deeply_indented_fence_does_not_close_the_block() -> None:
    """A fence line indented far beyond the opener is content, not a closer."""

    document = "```\n        ```\n# Not a heading\n```\n# After\n"

    assert _titles(document) == ["After"]


def test_two_tildes_do_not_open_a_fence() -> None:
    """A tilde fence also needs at least three characters."""

    assert _titles("~~\n# Heading\n") == ["Heading"]


def test_closer_indented_four_spaces_past_the_opener_does_not_close() -> None:
    """The closer tolerance ends at three spaces beyond the opener."""

    document = "```\n    ```\n# Not a heading\n```\n# After\n"

    assert _titles(document) == ["After"]


def test_backtick_in_info_string_still_opens_a_tilde_fence() -> None:
    """Only backtick fences reject a backtick in the info string."""

    document = "~~~a`b\n# Not a heading\n~~~\n# After\n"

    assert _titles(document) == ["After"]


def test_relative_closer_tolerance_is_a_known_top_level_limit() -> None:
    """Pin the documented price of not modelling lists.

    On top level, CommonMark treats a bare fence line indented four spaces
    past the margin as code; the hook accepts it as the closer of a fence
    that was itself indented, so the next line is read as a heading.
    """

    document = "   ```\n      ```\n# Code\n   ```\n"

    with pytest.raises(update_doctoc.TocGenerationError):
        _titles(document)
    assert _titles("   ```\n      ```\n# Code\n") == ["Code"]


def test_main_leaves_file_untouched_when_a_fence_is_unclosed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hook fails loudly and does not rewrite the table of contents."""

    document = "\n".join(
        [
            update_doctoc.START_MARKER,
            update_doctoc.END_MARKER,
            "",
            "# Real",
            "",
            "```bash",
            "# Not a heading",
            "",
        ]
    )
    path = tmp_path / "AGENTS.md"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        update_doctoc.main([str(path)])

    assert excinfo.value.code == 2
    assert "never closed" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == document


def test_replace_toc_skips_headings_inside_code_blocks() -> None:
    """End-to-end: the rendered table of contents omits code block lines."""

    document = "\n".join(
        [
            update_doctoc.START_MARKER,
            update_doctoc.END_MARKER,
            "",
            "# Poetry lock file management",
            "",
            "```bash",
            "# 1. Edit pyproject.toml",
            "poetry lock",
            "```",
            "",
        ]
    )

    updated = update_doctoc._replace_toc(document)

    assert "- [Poetry lock file management](#poetry-lock-file-management)" in updated
    assert "1. Edit pyproject.toml" not in updated.split(update_doctoc.END_MARKER)[0]
