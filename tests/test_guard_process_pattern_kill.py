# tests/test_guard_process_pattern_kill.py
"""Guard against full-command-line process kills (``pkill -f``).

``pkill -f <pattern>`` selects by matching the *full command line* of every
process on the machine. That makes it lethal to the caller's own process tree:
any ancestor whose argv happens to carry the pattern is a valid target, and the
tool spares only itself, never its caller.

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

Only the ``-f``/``--full`` form is flagged. ``pkill --help`` distinguishes
``-f, --full`` ("use full process name to match") from ``-x, --exact`` ("match
exactly with the command name"), and the name-matching variants -- ``pkill -x``,
``pkill -P <ppid>``, ``killall``, ``taskkill /im`` -- cannot hit a process that
merely *mentions* the pattern. They are a different failure class and are let
through on purpose; flagging them would make this repository-wide guard cry wolf.

Calls are resolved against the module's own imports, not matched by method name.
This guard runs repository-wide, so a false positive does not annoy one author,
it blocks everyone: an unrelated ``runner.run(["killall", "workers"])`` must stay
invisible while ``import subprocess as sp; sp.run([...])`` must not.

Known limit: the scan is AST based and only sees *literals*. A command assembled
from variables (``subprocess.run([tool, "-f", pattern])``) or fetched at runtime
passes unseen, as does an ``os.exec*``/``os.spawn*`` invocation. That is the
deliberate trade for staying free of false positives. Literal forms are covered
in the three shapes that occur in practice -- argument list, ``shell=True``
string, and ``["sh", "-c", "..."]``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_ROOTS = (
    _REPO_ROOT / "custom_components" / "googlefindmy",
    _REPO_ROOT / "tests",
)

# Tools that CAN be told to select by full command line. ``killall`` is
# deliberately absent: it matches the command *name*, so it is the same class as
# ``pkill -x`` and ``taskkill /im``, both of which are let through below. Adding
# it would flag a form that cannot hit an unrelated ancestor.
_PATTERN_KILL_TOOLS = frozenset({"pkill"})

# Callables that hand their first argument to the OS as a command.
_SUBPROCESS_RUNNERS = frozenset({"run", "call", "check_call", "check_output", "Popen"})

# Callables that hand a whole command string to a shell.
_SHELL_FUNCS = frozenset({"system", "getoutput", "getstatusoutput"})

# Programs that execute their -c argument as shell code.
_SHELL_LAUNCHERS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_SHELL_LAUNCH_MIN_ARGS = 3  # launcher, -c, command

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


class _ModuleBindings(NamedTuple):
    """How a module reaches ``subprocess``/``os`` in its own namespace.

    Resolving calls against the real imports is what keeps the guard free of
    false positives: an unrelated ``runner.run([...])`` or ``scheduler.Popen(...)``
    must not be able to fail a repository-wide test.
    """

    subprocess_aliases: frozenset[str]
    os_aliases: frozenset[str]
    imported_runners: frozenset[str]
    imported_shell_funcs: frozenset[str]


def _collect_bindings(tree: ast.Module) -> _ModuleBindings:
    """Read the module's imports to learn which names really are subprocess/os."""
    subprocess_aliases: set[str] = set()
    os_aliases: set[str] = set()
    imported_runners: set[str] = set()
    imported_shell_funcs: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    subprocess_aliases.add(alias.asname or "subprocess")
                elif alias.name == "os":
                    os_aliases.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    if alias.name in _SUBPROCESS_RUNNERS:
                        imported_runners.add(alias.asname or alias.name)
                    elif alias.name in _SHELL_FUNCS:
                        imported_shell_funcs.add(alias.asname or alias.name)
            elif node.module == "os":
                for alias in node.names:
                    if alias.name in _SHELL_FUNCS:
                        imported_shell_funcs.add(alias.asname or alias.name)

    return _ModuleBindings(
        frozenset(subprocess_aliases),
        frozenset(os_aliases),
        frozenset(imported_runners),
        frozenset(imported_shell_funcs),
    )


def _is_subprocess_runner(func: ast.expr, bindings: _ModuleBindings) -> bool:
    """True only for a call that really reaches ``subprocess``."""
    if isinstance(func, ast.Attribute):
        return (
            func.attr in _SUBPROCESS_RUNNERS
            and isinstance(func.value, ast.Name)
            and func.value.id in bindings.subprocess_aliases
        )
    if isinstance(func, ast.Name):
        return func.id in bindings.imported_runners
    return False


def _is_shell_call(func: ast.expr, bindings: _ModuleBindings) -> bool:
    """True only for ``os.system``/``subprocess.getoutput`` and imported twins."""
    if isinstance(func, ast.Attribute):
        return (
            func.attr in _SHELL_FUNCS
            and isinstance(func.value, ast.Name)
            and func.value.id in (bindings.os_aliases | bindings.subprocess_aliases)
        )
    if isinstance(func, ast.Name):
        return func.id in bindings.imported_shell_funcs
    return False


def _has_shell_true(node: ast.Call) -> bool:
    """True when the call passes ``shell=True`` literally."""
    return any(
        keyword.arg == "shell"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in node.keywords
    )


def _selects_by_full_command_line(args: list[str]) -> bool:
    """True when the arguments ask for matching against the full command line.

    ``pkill --help`` distinguishes ``-f, --full`` ("use full process name to
    match") from ``-x, --exact`` ("match exactly with the command name"). Only
    the former can hit a process that merely *mentions* the pattern in its argv,
    which is the failure class this guard exists for. ``-f`` may be bundled with
    other short options (``-9f``), so short flags are inspected per character.
    """
    for arg in args:
        if arg == "--full":
            return True
        if arg.startswith("--"):
            continue
        if arg.startswith("-") and "f" in arg[1:]:
            return True
    return False


def _shell_string_offends(value: str) -> bool:
    """True when a shell string runs a pattern-kill tool with full-argv matching.

    The tool name alone is not enough: ``pkill -x chrome`` matches the command
    *name* and cannot hit an unrelated ancestor, so flagging it would be a false
    positive on a repository-wide guard.
    """
    tokens = value.split()
    for index, token in enumerate(tokens):
        if Path(token).name in _PATTERN_KILL_TOOLS and _selects_by_full_command_line(
            tokens[index + 1 :]
        ):
            return True
    return False


def _shell_command_argument_offends(argv: list[str]) -> bool:
    """True for ``["sh", "-c", "<hazardous>"]`` and equivalents.

    Only the command string of an explicit shell launcher is inspected. A
    mention anywhere else executes nothing -- ``["git", "commit", "-m",
    "pkill -f is unsafe"]`` is a commit message, not a process kill, and
    flagging it would be a false positive on a repository-wide guard.
    """
    if len(argv) < _SHELL_LAUNCH_MIN_ARGS or Path(argv[0]).name not in _SHELL_LAUNCHERS:
        return False
    try:
        command_index = argv.index("-c") + 1
    except ValueError:
        return False
    return command_index < len(argv) and _shell_string_offends(argv[command_index])


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

    bindings = _collect_bindings(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first_arg = node.args[0]

        if _is_subprocess_runner(node.func, bindings):
            argv = _literal_strings(first_arg)
            tool = argv[0] if argv else None
            if (
                tool is not None
                and Path(tool).name in _PATTERN_KILL_TOOLS
                and _selects_by_full_command_line(argv[1:])
            ):
                violations.append(f"{rel_path}:{node.lineno} {Path(tool).name} -f")
                continue

            # subprocess.run("pkill -f chrome", shell=True): the command is a
            # bare string. Without shell=True that string is treated as a
            # program *path*, so it is not an invocation of the tool at all.
            if (
                _has_shell_true(node)
                and isinstance(first_arg, ast.Constant)
                and isinstance(first_arg.value, str)
                and _shell_string_offends(first_arg.value)
            ):
                violations.append(f"{rel_path}:{node.lineno} shell string")
                continue

            # subprocess.run(["sh", "-c", "pkill -f chrome"]): the kill hides in
            # the command string of an explicit shell launcher. Only that
            # position is scanned -- a mention in any other argument (a commit
            # message, a grep pattern) executes nothing.
            if _shell_command_argument_offends(argv):
                violations.append(f"{rel_path}:{node.lineno} shell argument")
                continue

        # os.system("pkill -f x") / subprocess.getoutput("killall x")
        if (
            _is_shell_call(node.func, bindings)
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
        "Full-command-line process kill found:\n"
        + "\n".join(f"  {item}" for item in offenders)
        + "\n\nWHY THIS IS WRONG\n"
        "  'pkill -f' matches the FULL command line of every process,\n"
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


_IMPORTS = "import os\nimport subprocess\n"


def test_guard_detects_a_synthetic_violation() -> None:
    """The detector must fire on the real form and stay quiet on look-alikes."""
    offending = _IMPORTS + 'subprocess.run(["pkill", "-f", "chrome"], check=False)\n'
    assert find_violations(offending, "x.py") == ["x.py:3 pkill -f"]

    quiet = (
        _IMPORTS
        + '"""A docstring that mentions subprocess.run(["pkill", "-f", "x"])."""\n'
        '# subprocess.run(["pkill", "-f", "chrome"])\n'
        'subprocess.run(["taskkill", "/f", "/im", "chrome.exe"], check=False)\n'
        'subprocess.run(["pgrep", "-f", "chrome"], check=False)\n'
        'subprocess.run(["pkill", "-x", "chrome"], check=False)\n'
        'subprocess.run(["pkill", "-P", "4242"], check=False)\n'
        'subprocess.run(["killall", "chrome"], check=False)\n'
        "subprocess.run([tool, '-f', pattern], check=False)\n"
    )
    assert find_violations(quiet, "x.py") == []


def test_guard_sees_the_shell_string_and_sh_c_forms() -> None:
    """The two literal escapes around an argv list must not slip through.

    Both were blind spots in the first draft of this guard and are the most
    obvious way the pattern would come back.
    """

    shell_string = _IMPORTS + 'subprocess.run("pkill -f chrome", shell=True)\n'
    assert find_violations(shell_string, "x.py") == ["x.py:3 shell string"]

    check_output = (
        _IMPORTS + 'subprocess.check_output("pkill --full chrome", shell=True)\n'
    )
    assert find_violations(check_output, "x.py") == ["x.py:3 shell string"]

    sh_c = _IMPORTS + 'subprocess.run(["sh", "-c", "pkill -f chrome"])\n'
    assert find_violations(sh_c, "x.py") == ["x.py:3 shell argument"]

    # Without shell=True the string is a program *path*, not a shell command.
    assert find_violations(_IMPORTS + 'subprocess.run("pkill -f x")\n', "x.py") == []

    # The sanctioned lookup must stay quiet in every one of those shapes.
    assert (
        find_violations(_IMPORTS + 'subprocess.run("pgrep -f c", shell=True)\n', "x.py")
        == []
    )
    assert (
        find_violations(
            _IMPORTS + 'subprocess.run(["sh", "-c", "pgrep -f x"])\n', "x.py"
        )
        == []
    )


def test_guard_ignores_unrelated_objects_with_the_same_method_names() -> None:
    """A foreign ``.run``/``.Popen``/``.system`` must never fail the whole suite.

    The guard runs repository-wide, so a false positive does not annoy one
    author, it blocks everyone. Calls are therefore resolved against the
    module's actual imports rather than matched by method name. Codex flagged
    exactly this on the first version: ``runner.run(["killall", "workers"])``
    made the guard fail on a module that never touches ``subprocess``.
    """

    unrelated = (
        _IMPORTS + 'runner.run(["pkill", "-f", "workers"], check=False)\n'
        'scheduler.Popen(["pkill", "-f", "job"])\n'
        'config.system("pkill -f x")\n'
        'self.check_output(["pkill", "-f", "y"])\n'
    )
    assert find_violations(unrelated, "x.py") == []

    # An aliased import still resolves, so the escape does not become a hole.
    aliased = (
        "import subprocess as sp\n"
        "from subprocess import run as _run\n"
        'sp.run(["pkill", "-f", "chrome"])\n'
        '_run(["pkill", "--full", "chrome"])\n'
    )
    assert find_violations(aliased, "x.py") == ["x.py:3 pkill -f", "x.py:4 pkill -f"]


def test_guard_only_scans_the_command_string_of_a_shell_launcher() -> None:
    """A mention in any other argument executes nothing and must stay quiet.

    Codex flagged the earlier version, which scanned every later literal
    argument: a commit message or a grep pattern that happens to contain
    "pkill -f" would have failed the repository-wide guard.
    """

    for safe in (
        'subprocess.run(["git", "commit", "-m", "pkill -f is unsafe"])\n',
        'subprocess.run(["grep", "-r", "pkill -f", "."])\n',
        'subprocess.run(["sh", "-c", "pkill -x chrome"])\n',
        'subprocess.run(["sh", "pkill -f chrome"])\n',  # no -c: not shell code
    ):
        assert find_violations(_IMPORTS + safe, "x.py") == [], safe

    for hazardous in (
        'subprocess.run(["sh", "-c", "pkill -f chrome"])\n',
        'subprocess.run(["bash", "-c", "pkill --full chrome"])\n',
        'subprocess.run(["/bin/sh", "-c", "pkill -f chrome"])\n',
    ):
        assert find_violations(_IMPORTS + hazardous, "x.py") == [
            "x.py:3 shell argument"
        ], hazardous


def test_guard_reports_an_unparseable_file_instead_of_skipping_it() -> None:
    """A syntax error must surface as a finding, not vanish into a bare except."""
    findings = find_violations("def broken(:\n", "x.py")

    assert len(findings) == 1
    assert "unparseable" in findings[0]


def test_guard_lets_name_matching_variants_through() -> None:
    """Only full-argv matching is the failure class; name matching is not.

    ``pkill --help`` distinguishes ``-f, --full`` from ``-x, --exact``. A
    name-matching call cannot hit a process that merely mentions the pattern in
    its arguments, so flagging it on a repository-wide guard would be a false
    positive. Codex flagged the earlier, tool-name-only version for exactly this.
    """

    for safe in (
        'subprocess.run(["pkill", "-x", "chrome"])\n',
        'subprocess.run(["pkill", "-P", "4242"])\n',
        'subprocess.run(["pkill", "chrome"])\n',
        'subprocess.run(["killall", "chrome"])\n',
        'subprocess.run("pkill -x chrome", shell=True)\n',
        'os.system("pkill -x chrome")\n',
    ):
        assert find_violations(_IMPORTS + safe, "x.py") == [], safe

    for hazardous in (
        'subprocess.run(["pkill", "-f", "chrome"])\n',
        'subprocess.run(["pkill", "--full", "chrome"])\n',
        'subprocess.run(["pkill", "-9f", "chrome"])\n',  # bundled short options
    ):
        assert find_violations(_IMPORTS + hazardous, "x.py") != [], hazardous


def test_shell_string_kills_are_detected() -> None:
    """``os.system`` and friends bypass the argv list but not this guard."""
    assert find_violations(_IMPORTS + 'os.system("pkill -f chrome")\n', "x.py") == [
        "x.py:3 shell command"
    ]
    assert find_violations(_IMPORTS + 'os.system("echo pkill-safe")\n', "x.py") == []
