# tests/test_pr_template_contract.py
"""Keep the pull request template and the maintainer body in line with the repository.

Two files describe a pull request:

* ``.github/PULL_REQUEST_TEMPLATE.md`` is what GitHub pre-fills for contributors.
* ``.github/MAINTAINER_PR_BODY.md`` is what maintainers copy and pass to
  ``gh pr create --body-file``. After its own leading comment it repeats the
  contributor template verbatim and adds "Maintainer verification" before
  "Evidence". It must not sit in a location GitHub reads as a template, or
  contributors would be offered it.

Both files restate facts that live elsewhere, and each restatement can go stale
without anyone noticing:

* the commit types and scopes of ``.github/workflows/semantic-pr.yml``;
* the ``make`` targets of the ``Makefile`` and the files under ``script/``;
* the coverage floor ``fail_under`` in ``pyproject.toml``;
* the template locations GitHub reads: a file named ``pull_request_template``
  (any case, any extension) in the repository root, ``docs`` or ``.github``, or
  any file in a ``PULL_REQUEST_TEMPLATE`` directory there. Sources:
  https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/about-issue-and-pull-request-templates
  and
  https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository

The root ``AGENTS.md`` bullet "PR template alignment" names both files and this test.

Most checks are small functions that return a list of problems; the guards assert
an empty list on the real files, and ``test_extractors_find_planted_errors`` plants
a mistake for each branch of these functions, so a reader that silently finds
nothing fails. The ``AGENTS.md`` check is inline and reads only the real file.

Blind spot: the checks read Markdown as text and do not render it. They prove that
the expected items are present and consistent, not that a filled-in pull request is
truthful. Checklist wording is pinned only by the short phrases in
``REQUIRED_PHRASES``; the maintainer body is held verbatim to the template.
GitHub's template detection is checked offline through the naming rule above, not
by asking GitHub. Commands are checked inside backtick spans and fenced code blocks
(backticks or tildes, indented up to three spaces) only.
"""

from __future__ import annotations

import re
from itertools import zip_longest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRIBUTOR_TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
MAINTAINER_BODY = REPO_ROOT / ".github" / "MAINTAINER_PR_BODY.md"
SEMANTIC_PR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "semantic-pr.yml"
MAKEFILE = REPO_ROOT / "Makefile"
PYPROJECT = REPO_ROOT / "pyproject.toml"
AGENTS_MD = REPO_ROOT / "AGENTS.md"

TEMPLATE_DIRS = ("", "docs", ".github")
TEMPLATE_STEM = "pull_request_template"
MAINTAINER_SECTION = "## Maintainer verification"
MAINTAINER_PREAMBLE = "<!--\nMaintainer pull request body."
TYPE_SECTION = "## Type of change"
CONDITIONAL_SECTION = "## Only if applicable"
CONDITIONAL_BLOCKS = ("Authentication and token cache", "Config flow", "Dependencies")
MAINTAINER_FIELDS = 8
MIN_CONTRIBUTOR_TASKS = 22
CONTRIBUTOR_SECTIONS = (
    "## Breaking change",
    "## Proposed change",
    "## Type of change",
    "## Linked issues",
    "## Checklist",
    "## Deprecation check",
    "## Only if applicable",
    "## Evidence (optional)",
)
REQUIRED_PHRASES = (
    "I understand the code",
    "If an AI tool generated",
    "no e-mail addresses",
    "locations or coordinates",
    "Wi-Fi names",
    "device or account IDs",
    "tokens",
)
MAINTAINER_ONLY = ("Codex", "home-assistant-tests")

_TASK_RE = re.compile(r"^[ \t]*[-*+] \[[ xX]\] ", re.MULTILINE)
_TYPE_RE = re.compile(r"^- \[ \] `(\w+)`", re.MULTILINE)
_HEADING_RE = re.compile(r"^## .+$", re.MULTILINE)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(
    r"^[ \t]{0,3}(```|~~~)[^\n]*\n(.*?)^[ \t]{0,3}\1", re.MULTILINE | re.DOTALL
)
_CHECKED_RE = re.compile(r"^[ \t]*[-*+] \[[xX]\] .*$", re.MULTILINE)
_MAKE_RE = re.compile(r"\bmake ([\w.-]+)")
_SCRIPT_FILE_RE = re.compile(r"\bscript/([\w/.-]+\.py)\b")
_SCRIPT_MODULE_RE = re.compile(r"-m script\.(\w+)")
_GATE_RE = re.compile(r"\bgate (?:is )?(\d+) ?%", re.IGNORECASE)
_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9_][\w.-]*):", re.MULTILINE)
_FAIL_UNDER_RE = re.compile(r"^fail_under\s*=\s*(\d+)\s*$", re.MULTILINE)
_BOX_GLYPHS = "☐☑☒"


def _both() -> list[Path]:
    return [CONTRIBUTOR_TEMPLATE, MAINTAINER_BODY]


def _section(text: str, heading: str) -> str | None:
    """Return the body of a level-two section, or None if the heading is absent."""
    match = re.search(
        rf"^{re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    return None if match is None else match.group(1)


def _code_spans(text: str) -> list[str]:
    """Return backtick spans outside fences plus every line inside fenced blocks."""
    spans = _BACKTICK_RE.findall(_FENCE_RE.sub("", text))
    for _, block in _FENCE_RE.findall(text):
        spans += block.splitlines()
    return spans


def _semantic_pr_lists() -> tuple[set[str], set[str]]:
    workflow = yaml.safe_load(SEMANTIC_PR_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["validate-pr-title"]["steps"]
    with_blocks = [step["with"] for step in steps if "with" in step]
    assert len(with_blocks) == 1, "expected exactly one step with a 'with' block"
    block = with_blocks[0]
    return set(block["types"].split()), set(block["scopes"].split())


# --- Pure checks: each returns a list of problems for one text. -------------------


def task_list_problems(text: str, min_tasks: int = 0) -> list[str]:
    """Report box glyphs, pre-checked items, and too few open items."""
    problems = [f"box glyph {glyph!r}" for glyph in _BOX_GLYPHS if glyph in text]
    problems += [f"pre-checked item: {line}" for line in _CHECKED_RE.findall(text)]
    count = len(_TASK_RE.findall(text)) - len(_CHECKED_RE.findall(text))
    if count < min_tasks:
        problems.append(f"{count} task-list items, expected at least {min_tasks}")
    return problems


def coverage_problems(text: str, floor: int) -> list[str]:
    """Report gate figures other than the floor, a missing figure, or a checked target."""
    figures = [int(value) for value in _GATE_RE.findall(text)]
    problems = [
        f"gate {value} %, pyproject says {floor} %"
        for value in figures
        if value != floor
    ]
    if not figures:
        problems.append("no 'gate <n> %' figure")
    for line in text.splitlines():
        if _TASK_RE.match(line) and re.search(r"\b(95|100)\b", line):
            problems.append(f"coverage target as a checkbox: {line}")
    return problems


def command_problems(text: str, make_targets: set[str], repo_root: Path) -> list[str]:
    """Report commands in code whose make target or script file does not exist."""
    problems: list[str] = []
    for span in _code_spans(text):
        problems += [
            f"make target {t!r}"
            for t in _MAKE_RE.findall(span)
            if t not in make_targets
        ]
        for rel in _SCRIPT_FILE_RE.findall(span):
            if not (repo_root / "script" / rel).is_file():
                problems.append(f"script file script/{rel}")
        for module in _SCRIPT_MODULE_RE.findall(span):
            if not (repo_root / "script" / f"{module}.py").is_file():
                problems.append(f"script module script.{module}")
    return problems


def type_problems(text: str, types: set[str]) -> list[str]:
    """Report differences between the type checkboxes and the allowed types."""
    section = _section(text, TYPE_SECTION)
    if section is None:
        return [f"no {TYPE_SECTION!r} section"]
    found = _TYPE_RE.findall(section)
    problems = [
        f"duplicate type {t!r}"
        for t in sorted({t for t in found if found.count(t) > 1})
    ]
    problems += [
        f"type {t!r} not in semantic-pr.yml" for t in sorted(set(found) - types)
    ]
    problems += [f"type {t!r} missing" for t in sorted(types - set(found))]
    return problems


def scope_problems(text: str, scopes: set[str]) -> list[str]:
    """Report differences between the 'Scopes:' list and the allowed scopes."""
    match = re.search(r"^Scopes:(.*?)\.", text, re.MULTILINE | re.DOTALL)
    if match is None:
        return ["no 'Scopes:' list"]
    found = {s for s in re.split(r"[\s,]+", match.group(1)) if s}
    problems = [f"scope {s!r} not in semantic-pr.yml" for s in sorted(found - scopes)]
    problems += [f"scope {s!r} missing" for s in sorted(scopes - found)]
    return problems


def first_heading_problems(text: str) -> list[str]:
    """Report a first level-two heading other than 'Breaking change'."""
    headings = _HEADING_RE.findall(text)
    if not headings or headings[0] != "## Breaking change":
        return [f"first heading is {headings[:1]}"]
    return []


def section_order_problems(contributor: str, maintainer: str) -> list[str]:
    """Report a maintainer body that does not repeat the contributor sections in order."""
    expected = _HEADING_RE.findall(contributor)
    found = [h for h in _HEADING_RE.findall(maintainer) if h != MAINTAINER_SECTION]
    problems = [] if found == expected else [f"sections {found}, expected {expected}"]
    body = maintainer.split(MAINTAINER_SECTION + "\n", 1)
    if len(body) != 2:
        return [*problems, f"no {MAINTAINER_SECTION!r} section"]
    section = body[1].split("\n## ", 1)[0]
    fields = [line for line in section.splitlines() if line.startswith("- ")]
    if len(fields) != MAINTAINER_FIELDS:
        problems.append(
            f"{len(fields)} maintainer fields, expected {MAINTAINER_FIELDS}"
        )
    after = body[1].split("\n## ", 1)
    if len(after) != 2 or not after[1].startswith("Evidence (optional)"):
        problems.append(
            f"{MAINTAINER_SECTION!r} is not directly before '## Evidence (optional)'"
        )
    return problems


def mirror_problems(contributor: str, maintainer: str) -> list[str]:
    """Report a maintainer body that is not its comment + the template + verification."""
    if not maintainer.startswith(MAINTAINER_PREAMBLE):
        return ["maintainer body does not start with its own comment"]
    rest = maintainer.split("-->\n\n", 1)
    if len(rest) != 2:
        return ["maintainer comment is not followed by a blank line"]
    stripped = re.sub(
        rf"^{re.escape(MAINTAINER_SECTION)}\n.*?(?=^## )",
        "",
        rest[1],
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    if stripped == contributor:
        return []
    pairs = zip_longest(stripped.splitlines(), contributor.splitlines())
    line = next((n for n, (a, b) in enumerate(pairs, 1) if a != b), None)
    return [f"maintainer body differs from the template (template line {line})"]


def conditional_problems(text: str) -> list[str]:
    """Report MUST, or a conditional block outside 'Only if applicable'."""
    problems = ["'MUST' in the template"] if re.search(r"\bMUST\b", text) else []
    section = _section(text, CONDITIONAL_SECTION) or ""
    for block in CONDITIONAL_BLOCKS:
        heading = f"### {block}\n"
        if heading not in section or text.count(heading) != 1:
            problems.append(f"{block!r} is not once under {CONDITIONAL_SECTION!r}")
    return problems


def content_problems(text: str) -> list[str]:
    """Report a section list other than the fixed one, a missing phrase, a maintainer term."""
    problems = []
    headings = tuple(_HEADING_RE.findall(text))
    if headings != CONTRIBUTOR_SECTIONS:
        problems.append(f"sections {headings}, expected {CONTRIBUTOR_SECTIONS}")
    problems += [f"missing {p!r}" for p in REQUIRED_PHRASES if p not in text]
    problems += [f"maintainer term {m!r}" for m in MAINTAINER_ONLY if m in text]
    return problems


def template_locations(repo_root: Path) -> set[str]:
    """Return every file GitHub would read as a pull request template."""
    found: set[str] = set()
    for rel in TEMPLATE_DIRS:
        base = repo_root / rel
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_file() and child.name.split(".")[0].lower() == TEMPLATE_STEM:
                found.add(child.relative_to(repo_root).as_posix())
            elif child.is_dir() and child.name.lower() == TEMPLATE_STEM:
                found.update(
                    f.relative_to(repo_root).as_posix()
                    for f in child.rglob("*")
                    if f.is_file()
                )
    return found


# --- Guards on the real files. -------------------------------------------------


def test_checkboxes_are_task_list_items() -> None:
    """Both files use GitHub task-list items, never box glyphs.

    Retire when: GitHub renders box glyphs as checkboxes, which it does not today.
    """
    problems = {
        CONTRIBUTOR_TEMPLATE.name: task_list_problems(
            CONTRIBUTOR_TEMPLATE.read_text(encoding="utf-8"), MIN_CONTRIBUTOR_TASKS
        ),
        MAINTAINER_BODY.name: task_list_problems(
            MAINTAINER_BODY.read_text(encoding="utf-8")
        ),
    }
    assert problems == {CONTRIBUTOR_TEMPLATE.name: [], MAINTAINER_BODY.name: []}


def test_coverage_floor_matches_pyproject() -> None:
    """The gate named in both files equals ``fail_under``; targets are no checkboxes.

    Retire when: the files stop naming a coverage figure.
    """
    match = _FAIL_UNDER_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    assert match is not None, "fail_under not found in pyproject.toml"
    floor = int(match.group(1))
    problems = {
        p.name: coverage_problems(p.read_text(encoding="utf-8"), floor) for p in _both()
    }
    assert problems == {p.name: [] for p in _both()}


def test_named_commands_exist() -> None:
    """Every backticked ``make`` target and ``script/`` command exists.

    Retire when: the files stop naming commands.
    """
    targets = set(_MAKE_TARGET_RE.findall(MAKEFILE.read_text(encoding="utf-8")))
    problems = {
        p.name: command_problems(p.read_text(encoding="utf-8"), targets, REPO_ROOT)
        for p in _both()
    }
    assert problems == {p.name: [] for p in _both()}


def test_type_checkboxes_match_semantic_pr() -> None:
    """The type checkboxes list exactly the types the title check accepts.

    Retire when: the title check is removed from ``.github/workflows``.
    """
    types, _ = _semantic_pr_lists()
    problems = {
        p.name: type_problems(p.read_text(encoding="utf-8"), types) for p in _both()
    }
    assert problems == {p.name: [] for p in _both()}


def test_scope_list_matches_semantic_pr() -> None:
    """The 'Scopes:' list names exactly the scopes the title check accepts.

    Retire when: the title check is removed from ``.github/workflows``.
    """
    _, scopes = _semantic_pr_lists()
    problems = {
        p.name: scope_problems(p.read_text(encoding="utf-8"), scopes) for p in _both()
    }
    assert problems == {p.name: [] for p in _both()}


def test_breaking_change_comes_first() -> None:
    """'Breaking change' is the first section, so release notes can quote it.

    Retire when: release notes stop being drafted from pull request bodies.
    """
    problems = {
        p.name: first_heading_problems(p.read_text(encoding="utf-8")) for p in _both()
    }
    assert problems == {p.name: [] for p in _both()}


def test_maintainer_body_is_not_a_template_location() -> None:
    """The maintainer body exists and GitHub offers only the contributor template.

    Retire when: GitHub changes its template locations; then update
    ``template_locations`` from the documentation instead of retiring.
    """
    assert MAINTAINER_BODY.is_file(), f"{MAINTAINER_BODY.name} is missing"
    assert template_locations(REPO_ROOT) == {".github/PULL_REQUEST_TEMPLATE.md"}


def test_maintainer_body_repeats_contributor_sections() -> None:
    """The maintainer body is a superset: same sections, same order, plus verification.

    Retire when: maintainers stop opening pull requests from this body.
    """
    assert MAINTAINER_BODY.is_file(), f"{MAINTAINER_BODY.name} is missing"
    problems = section_order_problems(
        CONTRIBUTOR_TEMPLATE.read_text(encoding="utf-8"),
        MAINTAINER_BODY.read_text(encoding="utf-8"),
    )
    assert problems == []


def test_maintainer_body_mirrors_contributor_template() -> None:
    """Apart from its comment and verification, the maintainer body is the template.

    Retire when: maintainers stop opening pull requests from this body.
    """
    problems = mirror_problems(
        CONTRIBUTOR_TEMPLATE.read_text(encoding="utf-8"),
        MAINTAINER_BODY.read_text(encoding="utf-8"),
    )
    assert problems == []


def test_conditional_blocks_stay_optional() -> None:
    """Auth, config flow and dependency items sit under 'Only if applicable'; no MUST.

    Retire when: the template stops having an 'Only if applicable' section.
    """
    problems = conditional_problems(CONTRIBUTOR_TEMPLATE.read_text(encoding="utf-8"))
    assert problems == []


def test_contributor_template_content() -> None:
    """Fixed section list, required checklist phrases, no maintainer-only terms.

    Retire when: the template is restructured on purpose; then update the tuples
    ``CONTRIBUTOR_SECTIONS``, ``REQUIRED_PHRASES`` and ``MAINTAINER_ONLY``.
    """
    problems = content_problems(CONTRIBUTOR_TEMPLATE.read_text(encoding="utf-8"))
    assert problems == []


def test_agents_md_names_both_files() -> None:
    """The 'PR template alignment' bullet names both files and this test.

    Retire when: the bullet is removed from ``AGENTS.md``.
    """
    lines = [
        line
        for line in AGENTS_MD.read_text(encoding="utf-8").splitlines()
        if "**PR template alignment.**" in line
    ]
    assert len(lines) == 1, (
        f"expected one 'PR template alignment' bullet, found {len(lines)}"
    )
    for needle in (
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/MAINTAINER_PR_BODY.md",
        "tests/test_pr_template_contract.py",
    ):
        assert needle in lines[0], f"{needle} missing from the bullet"


# --- Control: the checks must see planted mistakes. ------------------------------


def test_extractors_find_planted_errors(tmp_path: Path) -> None:
    """Each check reports a planted mistake, so a blind reader cannot pass.

    Retire when: the guards above are retired.
    """
    assert task_list_problems("- ☐ x\n- [ ] `fix`: y\n", 1) == ["box glyph '☐'"]
    assert task_list_problems("- [ ] `fix`: bug fix\n", 2)
    assert not task_list_problems("* [ ] a\n  - [ ] b\n", 2)
    assert task_list_problems("- [ ] a\n- [x] b\n", 1) == ["pre-checked item: - [x] b"]
    assert task_list_problems("- [ ] a\n* [X] b\n", 2)
    assert coverage_problems("The CI gate is 81 %.", 80)
    assert coverage_problems("no figure here", 80)
    gate = "The CI gate is 80 %.\n"
    assert coverage_problems(gate + "- [ ] Coverage reaches 95 %.\n", 80)
    assert coverage_problems(gate + "* [ ] Coverage reaches 95 %.\n", 80)
    assert coverage_problems(gate + "  - [ ] Coverage reaches 100 %.\n", 80)
    (tmp_path / "script").mkdir()
    dead_make = (
        "Run `make nope`."  # planted dead target, admitted in test_guard_make_targets
    )
    assert command_problems(dead_make, {"preflight"}, tmp_path)
    assert command_problems("Run `python script/nope.py`.", set(), tmp_path)
    assert command_problems("Run `python3 -m script.nope`.", set(), tmp_path)
    assert command_problems("```\npython script/nope.py\n```\n", set(), tmp_path)
    assert command_problems("~~~\npython script/nope.py\n~~~\n", set(), tmp_path)
    assert command_problems("  ```\npython script/nope.py\n  ```\n", set(), tmp_path)
    types = TYPE_SECTION + "\n"
    assert type_problems(types + "- [ ] `fix`: x\n", {"fix", "feat"})
    assert type_problems(types + "- [ ] `fix`: x\n- [ ] `wip`: y\n", {"fix"})
    assert type_problems(types + "- [ ] `fix`: x\n- [ ] `fix`: y\n", {"fix"})
    assert type_problems("- [ ] `fix`: x\n", {"fix"})
    other = "\n## Checklist\n- [ ] `README` updated\n"
    assert not type_problems(types + "- [ ] `fix`: x\n" + other, {"fix"})
    assert scope_problems("Scopes: api,\nci.\n", {"api", "ci", "deps"})
    assert scope_problems("no list", {"api"})
    assert first_heading_problems("## Proposed change\n## Breaking change\n")
    assert first_heading_problems("## Breaking change\n## Proposed change\n") == []
    assert section_order_problems("## A\n## B\n", "## B\n## A\n")
    assert section_order_problems("## A\n", "## A\n")
    contributor = "## A\n## Evidence (optional)\n"
    fields = "".join(f"- f{i}\n" for i in range(8))
    maintainer = f"## A\n{MAINTAINER_SECTION}\n{fields}\n## Evidence (optional)\n"
    assert section_order_problems(contributor, maintainer) == []
    assert len(section_order_problems("## B\n" + contributor, maintainer)) == 1
    assert (
        len(section_order_problems(contributor, maintainer.replace("- f7\n", ""))) == 1
    )
    moved = f"{MAINTAINER_SECTION}\n{fields}\n## A\n## Evidence (optional)\n"
    assert len(section_order_problems(contributor, moved)) == 1
    template = "<!--\nhello\n-->\n\n" + contributor
    preamble = MAINTAINER_PREAMBLE + "\n-->\n\n"
    body = preamble + template.replace(
        "## Evidence", f"{MAINTAINER_SECTION}\n- f\n\n## Evidence"
    )
    assert mirror_problems(template, body) == []
    assert mirror_problems(template, body.replace("hello", "hallo"))
    assert mirror_problems(template, body.replace(MAINTAINER_PREAMBLE, "<!--\nx"))
    assert mirror_problems(template, body.replace("-->\n\n<!--", "-->\n<!--"))
    assert mirror_problems(template, MAINTAINER_PREAMBLE + "\n-->\n## A\n") == [
        "maintainer comment is not followed by a blank line"
    ]
    assert "line 5" in mirror_problems(template, body.replace("## A\n", ""))[0]
    assert "None" not in mirror_problems(template, body + "## Z\n")[0]
    sections = "".join(f"{h}\n" for h in CONTRIBUTOR_SECTIONS)
    phrases = "".join(f"- [ ] {p}\n" for p in REQUIRED_PHRASES)
    assert content_problems(sections + phrases) == []
    assert content_problems(sections.replace("## Linked issues\n", "") + phrases)
    assert content_problems(sections + phrases.replace("I understand", "I read"))
    assert content_problems(sections + phrases + "- [ ] Codex review passed.\n")
    blocks = "".join(f"### {b}\n" for b in CONDITIONAL_BLOCKS)
    optional = f"## Checklist\n{CONDITIONAL_SECTION}\n{blocks}"
    assert conditional_problems(optional) == []
    assert conditional_problems(optional + "You MUST test.\n")
    assert conditional_problems(optional.replace("### Config flow\n", ""))
    assert conditional_problems("### Dependencies\n" + optional)
    assert conditional_problems(f"### Dependencies\n{CONDITIONAL_SECTION}\n")
    (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE").mkdir(parents=True)
    (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "extra.md").write_text("x")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "Pull_Request_Template.txt").write_text("x")
    assert template_locations(tmp_path) == {
        ".github/PULL_REQUEST_TEMPLATE/extra.md",
        "docs/Pull_Request_Template.txt",
    }
