# script/precommit_hooks/update_doctoc.py
"""Generate DocToc-compatible tables without requiring network access."""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

START_MARKER = "<!-- START doctoc generated TOC please keep comment here to allow auto update -->"
END_MARKER = "<!-- END doctoc generated TOC please keep comment here to allow auto update -->"
HEADER_LINE = "**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*"

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")
_PERCENT_ESCAPE_RE = re.compile(r"%([a-fA-F]|\d){2}")
_REMOVE_CHARS = "/?!:[]`.,()*\"';{}+=<>~$|#@&–—\\"
_REMOVE_TRANSLATION = str.maketrans("", "", _REMOVE_CHARS)
_CJK_PUNCTUATION = set("。？！、；：“”【】（）〔〕［］﹃﹄‘’﹁﹂—…－～《》〈〉「」")


class TocGenerationError(RuntimeError):
    """Raised when the DocToc markers are missing or malformed."""


def _iter_headings(lines: Iterable[str]) -> list[tuple[int, str]]:
    """Collect Markdown headings and their levels in document order."""

    headings: list[tuple[int, str]] = []
    for raw_line in lines:
        match = _HEADING_RE.match(raw_line)
        if not match:
            continue
        level = len(match.group("hashes"))
        title = match.group("title").strip()
        if title:
            headings.append((level, title))
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
    toc_lines = [START_MARKER, "<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->", HEADER_LINE, ""]

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
