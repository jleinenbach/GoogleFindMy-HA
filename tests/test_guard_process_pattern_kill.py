# tests/test_guard_process_pattern_kill.py
"""Guard against command-line-pattern process kills (``pkill``/``killall``).

``pkill -f <pattern>`` and ``killall`` select by matching the *full command line*
of every process on the machine. That makes them lethal to the caller's own
process tree: any ancestor whose argv happens to carry the pattern is a valid
target, and the tool spares only itself, never its caller.

Measured on 2026-07-28 in this repository: ``chrome_driver.py`` ran
``subprocess.run(["pkill", "-f", "chrome"])`` during the Chrome auth flow. A
pytest invocation that received ``tests/test_chrome_driver.py`` as an argument
therefore carried the word ``chrome`` in its own argv, and the CLI subprocess it
had started killed it -- exit 143 (SIGTERM), no traceback, no summary. The A/B
control differed only by a ``-k`` filter that matched nothing, i.e. by the word
in argv alone: without it exit 0, with it exit 143.

The correct construction is ``chrome_driver._terminate_matching_processes``: it
uses ``pgrep`` for the same selection, then filters out the own PID and the whole
ancestry before signalling. This guard keeps the broad form from coming back.

``taskkill`` is deliberately NOT flagged: ``taskkill /f /im chrome.exe`` matches
the *image name*, not a command line, so it cannot hit an unrelated process that
merely mentions the name. Different failure class, Windows-only branch.

Known limit: the scan is AST based and only sees *literals*. A command assembled
from variables (``subprocess.run([tool, "-f", pattern])``) or fetched at runtime
passes unseen, as does an ``os.exec*``/``os.spawn*`` invocation. That is the
deliberate trade: no false positives, because a guard that cries wolf gets
switched off. Literal forms are covered in all three shapes that occur in
practice -- argument list, ``shell=True`` string, and ``["sh", "-c", "..."]``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_ROOTS = (
    _REPO_ROOT / "custom_components" / "googlefindmy",
    _REPO_ROOT / "tests",
)

# Tools that select processes by matching against the command line.
_PATTERN_KILL_TOOLS = frozenset({"pkill", "killall"})

# Callables that hand their first argument to the OS as a command.
_SUBPROCESS_RUNNERS = frozenset({"run", "call", "check_call", "check_output", "Popen"})

# Pre-existing offenders. Empty by construction: the two historical call sites in
# ``chrome_driver.py`` were migrated to ``_terminate_matching_processes`` in the
# same change that added this guard, so there is nothing legitimate left to
# freeze. A new entry here needs a written reason, not a shrug.
LEGACY_ALLOWLIST: frozenset[str] = frozenset()


def _iter_python_files() -> list[tuple[Path, str]]:
    """Yield ``(path, repo_relative_path)`` for every scanned Python file."""
    own = Path(__file__).resolve()
    files: list[tuple[Path, str]] = []
    for root in _SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == own:
                continue  # this guard quotes the tool names in prose
            files.append((path, path.relative_to(_REPO_ROOT).as_posix()))
    return files


def _first_string_element(node: ast.expr) -> str | None:
    """Return the first element of a literal list/tuple if it is a string."""
    if not isinstance(node, ast.List | ast.Tuple) or not node.elts:
        return None
    first = node.elts[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _is_subprocess_runner(func: ast.expr) -> bool:
    """True for ``subprocess.run(...)`` and for a bare imported ``run(...)``."""
    if isinstance(func, ast.Attribute):
        return func.attr in _SUBPROCESS_RUNNERS
    if isinstance(func, ast.Name):
        return func.id in _SUBPROCESS_RUNNERS
    return False


def _shell_string_offends(value: str) -> bool:
    """True when a shell string invokes one of the pattern-kill tools."""
    return any(f"{tool} " in value for tool in _PATTERN_KILL_TOOLS)


def _literal_strings(node: ast.expr) -> list[str]:
    """Return the string constants of a literal list/tuple, in order."""
    if not isinstance(node, ast.List | ast.Tuple):
        return []
    return [
        element.value
        for element in node.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    ]


def find_violations(source: str, rel_path: str) -> list[str]:
    """Return ``"<rel_path>:<line> <tool>"`` for every pattern kill in *source*."""
    try:
        tree = ast.parse(source)
    except SyntaxError as err:  # a broken file is a finding, not a silent pass
        return [f"{rel_path}:{err.lineno or 0} unparseable ({err.msg})"]

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first_arg = node.args[0]

        if _is_subprocess_runner(node.func):
            tool = _first_string_element(first_arg)
            if tool is not None and Path(tool).name in _PATTERN_KILL_TOOLS:
                violations.append(f"{rel_path}:{node.lineno} {Path(tool).name}")
                continue

            # subprocess.run("pkill -f chrome", shell=True): the command is a
            # bare string, not an argv list.
            if (
                isinstance(first_arg, ast.Constant)
                and isinstance(first_arg.value, str)
                and _shell_string_offends(first_arg.value)
            ):
                violations.append(f"{rel_path}:{node.lineno} shell string")
                continue

            # subprocess.run(["sh", "-c", "pkill -f chrome"]): the kill hides in
            # a later element of an otherwise innocent argv list.
            if any(
                _shell_string_offends(element)
                for element in _literal_strings(first_arg)[1:]
            ):
                violations.append(f"{rel_path}:{node.lineno} shell argument")
                continue

        # os.system("pkill -f x") / subprocess.getoutput("killall x")
        func = node.func
        shell_call = isinstance(func, ast.Attribute) and func.attr in {
            "system",
            "getoutput",
            "getstatusoutput",
        }
        if (
            shell_call
            and isinstance(first_arg, ast.Constant)
            and isinstance(first_arg.value, str)
            and _shell_string_offends(first_arg.value)
        ):
            violations.append(f"{rel_path}:{node.lineno} shell command")

    return violations


def test_no_command_line_pattern_kills_in_the_tree() -> None:
    """No module may kill processes by command-line pattern."""
    offenders: list[str] = []
    for path, rel in _iter_python_files():
        if rel in LEGACY_ALLOWLIST:
            continue
        offenders.extend(find_violations(path.read_text(encoding="utf-8"), rel))

    assert not offenders, (
        "Command-line-pattern process kill found:\n"
        + "\n".join(f"  {item}" for item in offenders)
        + "\n\nWHY THIS IS WRONG\n"
        "  'pkill -f' / 'killall' match the FULL command line of every process,\n"
        "  including the caller's own ancestry. Measured here: a pytest run whose\n"
        "  argv contained 'tests/test_chrome_driver.py' was killed by the CLI\n"
        "  subprocess it had started (exit 143).\n"
        "\nHOW TO FIX\n"
        "  Use chrome_driver._terminate_matching_processes(pattern): same pgrep\n"
        "  selection, but the own PID and the whole ancestry are filtered out\n"
        "  before anything is signalled."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlisted file that was cleaned up (or vanished) must leave the list."""
    known = {rel for _, rel in _iter_python_files()}
    stale = sorted(
        rel
        for rel in LEGACY_ALLOWLIST
        if rel not in known
        or not find_violations((_REPO_ROOT / rel).read_text(encoding="utf-8"), rel)
    )
    assert not stale, (
        "Stale LEGACY_ALLOWLIST entries (file is clean now or no longer exists); "
        f"remove them to keep the backlog honest: {stale}"
    )


def test_guard_detects_a_synthetic_violation() -> None:
    """The detector must fire on the real form and stay quiet on look-alikes."""
    offending = 'subprocess.run(["pkill", "-f", "chrome"], check=False)\n'
    assert find_violations(offending, "x.py") == ["x.py:1 pkill"]

    quiet = (
        '"""A docstring that mentions subprocess.run(["pkill", "-f", "x"])."""\n'
        '# subprocess.run(["killall", "chrome"])\n'
        'subprocess.run(["taskkill", "/f", "/im", "chrome.exe"], check=False)\n'
        'subprocess.run(["pgrep", "-f", "chrome"], check=False)\n'
        "subprocess.run([tool, '-f', pattern], check=False)\n"
    )
    assert find_violations(quiet, "x.py") == []


def test_guard_sees_the_shell_string_and_sh_c_forms() -> None:
    """The two literal escapes around an argv list must not slip through.

    Both were blind spots in the first draft of this guard and are the most
    obvious way the pattern would come back.
    """

    shell_string = 'subprocess.run("pkill -f chrome", shell=True, check=False)\n'
    assert find_violations(shell_string, "x.py") == ["x.py:1 shell string"]

    check_output = 'subprocess.check_output("killall chrome", shell=True)\n'
    assert find_violations(check_output, "x.py") == ["x.py:1 shell string"]

    sh_c = 'subprocess.run(["sh", "-c", "pkill -f chrome"], check=False)\n'
    assert find_violations(sh_c, "x.py") == ["x.py:1 shell argument"]

    # The sanctioned lookup must stay quiet in every one of those shapes.
    assert (
        find_violations('subprocess.run("pgrep -f chrome", shell=True)\n', "x.py") == []
    )
    assert find_violations('subprocess.run(["sh", "-c", "pgrep -f x"])\n', "x.py") == []


def test_guard_reports_an_unparseable_file_instead_of_skipping_it() -> None:
    """A syntax error must surface as a finding, not vanish into a bare except."""
    findings = find_violations("def broken(:\n", "x.py")

    assert len(findings) == 1
    assert "unparseable" in findings[0]


def test_shell_string_kills_are_detected() -> None:
    """``os.system`` and friends bypass the argv list but not this guard."""
    assert find_violations('os.system("pkill -f chrome")\n', "x.py") == [
        "x.py:1 shell command"
    ]
    assert find_violations('os.system("echo pkill-safe")\n', "x.py") == []
