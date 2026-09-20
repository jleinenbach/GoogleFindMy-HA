# script/precommit_hooks/update_doctoc.py
"""Generate DocToc-compatible tables without requiring network access."""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote

START_MARKER = (
    "<!-- START doctoc generated TOC please keep comment here to allow auto update -->"
)
END_MARKER = (
    "<!-- END doctoc generated TOC please keep comment here to allow auto update -->"
)
HEADER_LINE = "**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*"

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")
_FENCE_RE = re.compile(r"^(?P<indent> *)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
# CommonMark allows up to three spaces of indentation before a fence.
_MAX_FENCE_INDENT = 3
_PERCENT_ESCAPE_RE = re.compile(r"%([a-fA-F]|\d){2}")
_REMOVE_CHARS = "/?!:[]`.,()*\"';{}+=<>~$|#@&–—\\"
_REMOVE_TRANSLATION = str.maketrans("", "", _REMOVE_CHARS)
_CJK_PUNCTUATION = set("。？！、；：“”【】（）〔〕［］﹃﹄‘’﹁﹂—…－～《》〈〉「」")


class TocGenerationError(RuntimeError):
    """Raised when the DocToc markers or the document structure are malformed."""


def _iter_headings(lines: Iterable[str]) -> list[tuple[int, str]]:
    """Collect Markdown headings and their levels in document order.

    Lines inside fenced code blocks are skipped: a shell comment such as
    ``# 1. Edit pyproject.toml`` is not a heading, and GitHub creates no anchor
    for it, so listing it in the table of contents yields a dead link.

    Fences follow CommonMark for blocks outside containers. Lists and block
    quotes are not modelled; a closing fence is therefore accepted with up to
    three spaces more indentation than its opener. That covers a fence inside
    a list item whose opener is indented at most three spaces; the price is
    that, on top level, a bare fence line indented four to six spaces inside a
    fence indented one to three spaces is taken as the closer. A fence that is
    still open at the end of the document raises ``TocGenerationError``
    instead of silently dropping every later heading.
    """

    headings: list[tuple[int, str]] = []
    # Character, length, indentation and line number of the open fence.
    open_fence: tuple[str, int, int, int] | None = None
    for line_number, raw_line in enumerate(lines, start=1):
        fence = _FENCE_RE.match(raw_line)
        if fence:
            marker = fence.group("fence")
            char, length = marker[0], len(marker)
            indent = len(fence.group("indent"))
            info = fence.group("info").strip()
            if open_fence is None:
                # An opening fence may carry an info string such as ``bash``,
                # but a backtick fence's info string may not contain a backtick.
                if indent <= _MAX_FENCE_INDENT and not (char == "`" and "`" in info):
                    open_fence = (char, length, indent, line_number)
                    continue
            elif (
                char == open_fence[0]
                and length >= open_fence[1]
                and indent <= open_fence[2] + _MAX_FENCE_INDENT
                and not info
            ):
                # A closing fence matches the opener and carries no info string.
                open_fence = None
                continue
        if open_fence is not None:
            continue
        match = _HEADING_RE.match(raw_line)
        if not match:
            continue
        level = len(match.group("hashes"))
        title = match.group("title").strip()
        if title:
            headings.append((level, title))
    if open_fence is not None:
        raise TocGenerationError(
            f"code fence opened on line {open_fence[3]} is never closed"
        )
    return headings


def _ascii_only_lower(text: str) -> str:
    """Lowercase ASCII characters without altering Unicode symbols."""

    return "".join(char.lower() if "A" <= char <= "Z" else char for char in text)


def _basic_github_id(text: str) -> str:
    """Mirror ``anchor-markdown-header`` GitHub slug generation."""

    text = text.replace(" ", "-")
    text = _PERCENT_ESCAPE_RE.sub("", text)
    text = text.translate(_REMOVE_TRANSLATION)
    text = "".join(char for char in text if char not in _CJK_PUNCTUATION)
    return text


def _slugify(title: str, seen: dict[str, int]) -> str:
    """Return a GitHub-compatible anchor slug for the provided heading."""

    normalized = _basic_github_id(_ascii_only_lower(title.strip()))
    if not normalized:
        normalized = title.lower()

    count = seen[normalized]
    seen[normalized] += 1
    if count:
        normalized = f"{normalized}-{count}"

    return quote(normalized, safe="-_.~")


def _render_toc(headings: list[tuple[int, str]]) -> list[str]:
    """Render DocToc-compatible Markdown for the collected headings."""

    seen_slugs: dict[str, int] = defaultdict(int)
    toc_lines = [
        START_MARKER,
        "<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->",
        HEADER_LINE,
        "",
    ]

    for level, title in headings:
        indent = "  " * (level - 1)
        slug = _slugify(title, seen_slugs)
        toc_lines.append(f"{indent}- [{title}](#{slug})")

    toc_lines.extend(["", END_MARKER])
    return toc_lines


def _replace_toc(content: str) -> str:
    """Return updated Markdown content with an inlined DocToc."""

    start_index = content.find(START_MARKER)
    end_index = content.find(END_MARKER)
    if start_index == -1 or end_index == -1 or end_index <= start_index:
        raise TocGenerationError("DocToc markers are missing or malformed")

    before = content[:start_index]
    after = content[end_index + len(END_MARKER) :]

    headings = _iter_headings(content.splitlines())
    toc_lines = _render_toc(headings)
    toc_block = "\n".join(toc_lines)

    # Ensure the section separation mirrors DocToc's blank-line convention.
    if before:
        before = before.rstrip("\n") + "\n"
        prefix = before
    else:
        prefix = ""
    after = after.lstrip("\n")
    return f"{prefix}{toc_block}\n\n{after}"


def update_file(path: Path) -> bool:
    """Update the DocToc section for ``path`` and return True when modified."""

    original = path.read_text(encoding="utf-8")
    updated = _replace_toc(original)
    if updated == original:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    """Entry point for the DocToc refresh pre-commit hook."""

    parser = argparse.ArgumentParser(description="Refresh in-repo DocToc tables")
    parser.add_argument("files", nargs="+", help="Markdown files managed by DocToc")
    args = parser.parse_args(argv)

    changed = False
    for filename in args.files:
        path = Path(filename)
        if not path.exists():
            continue
        try:
            if update_file(path):
                changed = True
        except TocGenerationError as exc:
            parser.error(f"{filename}: {exc}")

    if changed:
        print("DocToc updated; re-run pre-commit to verify staging.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
