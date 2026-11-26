# tests/test_login_launcher_lang.py
"""The launcher speaks the language of ``GFMY_LOCALE``, and ``--lang`` sets it (L-11).

``login.sh`` prints its own terminal lines in English by default and in German
for a ``de*`` tag. The tag comes from ``--lang`` (the short name), else from an
inherited ``GFMY_LOCALE``, else from the shell locale, which is the same value
the container hands to Chrome. Terminal and sign-in page therefore cannot
disagree. The noVNC viewer is out of scope on purpose: it follows the browser
language (decision NA-2), which the README states.

The behavioural tests run the REAL launcher against a stub ``docker``. The text
guard after them pins the one property a behavioural test cannot reach in
full: every ``[login]`` line goes through ``msg``/``pick``, so a new message
cannot quietly stay English-only.

``login.cmd`` does the same on Windows, with its German written in ASCII
transliteration (ae, oe, ue, ss) so no console code page has to change. It
cannot run here, so the last block pins it by text: adjacent EN/DE line pairs,
ASCII only, ``--lang`` read before any output, and a two-language help. Which
tags count as German is compared with the real ``login.sh``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_docker_login_hardening import (
    DOCKER_LOGIN,
    _launcher_sandbox,
    _run_launcher_on_tty,
)

_BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(_BASH is None, reason="bash runs the launcher")


def _run(
    work: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    assert _BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, test-local stub PATH
        [_BASH, str(work / "login.sh"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def _forwarded_locale(tmp_path: Path, extra_env: dict[str, str], *args: str) -> str:
    """Return the GFMY_LOCALE the stub ``docker`` sees."""

    work, env = _launcher_sandbox(tmp_path)
    (tmp_path / "bin" / "docker").write_text(
        '#!/usr/bin/env bash\nprintf "LOCALE=[%s]\\n" "${GFMY_LOCALE-unset}"\n',
        "utf-8",
    )
    proc = _run(work, {**env, **extra_env}, *args)
    assert proc.returncode == 0, proc.stderr
    return next(
        line[len("LOCALE=[") : -1]
        for line in proc.stdout.splitlines()
        if line.startswith("LOCALE=[")
    )


@pytest.mark.parametrize(
    ("args", "extra_env", "first_word"),
    [
        (("--lang", "de", "--help"), {}, "Aufruf:"),
        (("--lang=de-DE", "--help"), {}, "Aufruf:"),
        (("--help",), {"LANG": "de_DE.UTF-8"}, "Aufruf:"),
        (("--help",), {}, "Usage:"),
        (("--lang", "C", "--help"), {"LANG": "de_DE.UTF-8"}, "Usage:"),
        (("--help",), {"GFMY_LOCALE": "en", "LANG": "de_DE.UTF-8"}, "Usage:"),
        (("--lang", "de", "--help"), {"GFMY_LOCALE": "en"}, "Aufruf:"),
    ],
)
def test_help_follows_the_launcher_language(
    tmp_path: Path, args: tuple[str, ...], extra_env: dict[str, str], first_word: str
) -> None:
    """--lang wins over GFMY_LOCALE, which wins over the shell locale; C means English."""

    work, env = _launcher_sandbox(tmp_path)
    proc = _run(work, {**env, **extra_env}, *args)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(first_word), proc.stdout[:200]
    assert "--lang" in proc.stdout, "the help must document --lang"


def test_lang_reaches_messages_printed_while_parsing(tmp_path: Path) -> None:
    """--lang AFTER a bad --ip still turns the parser's own error German.

    This is why --lang is read in a pre-scan: the parser prints before it would
    ever reach a later --lang.
    """

    work, env = _launcher_sandbox(tmp_path)
    proc = _run(work, env, "--ip", "not-an-ip", "--lang", "de")
    assert proc.returncode == 2
    assert "braucht eine IP-Adresse" in proc.stderr, proc.stderr
    assert "needs an IP address" not in proc.stderr


def test_lang_after_double_dash_is_not_an_option(tmp_path: Path) -> None:
    work, env = _launcher_sandbox(tmp_path)
    proc = _run(work, env, "--ip", "not-an-ip", "--", "--lang", "de")
    assert proc.returncode == 2
    assert "needs an IP address" in proc.stderr, proc.stderr


def test_lang_without_a_value_is_refused(tmp_path: Path) -> None:
    work, env = _launcher_sandbox(tmp_path)
    proc = _run(work, env, "--lang")
    assert proc.returncode == 2
    assert "--lang needs a language tag" in proc.stderr
    assert "DOCKER_ARGS" not in proc.stdout, "nothing may start without a language"


@pytest.mark.parametrize(
    ("args", "extra_env", "expected"),
    [
        ((), {}, ""),
        (("--lang", "de"), {}, "de"),
        (("--lang=fr-CA",), {}, "fr-CA"),
        (("--lang", "de"), {"GFMY_LOCALE": "en"}, "de"),
        ((), {"GFMY_LOCALE": "en"}, "en"),
    ],
)
def test_lang_is_forwarded_as_gfmy_locale(
    tmp_path: Path, args: tuple[str, ...], extra_env: dict[str, str], expected: str
) -> None:
    """--lang is a short name for GFMY_LOCALE, so Chrome gets the same value."""

    assert _forwarded_locale(tmp_path, extra_env, *args) == expected


def test_start_lines_are_german_with_lang_de(tmp_path: Path) -> None:
    work, env = _launcher_sandbox(tmp_path)
    german = _run(work, env, "--lang", "de")
    english = _run(work, env)
    assert german.returncode == english.returncode == 0
    assert "Starte den Anmelde-Container" in german.stdout
    assert "Starting the GoogleFindMy-HA login container" not in german.stdout
    assert "Starting the GoogleFindMy-HA login container" in english.stdout


def test_track_menu_is_german_on_a_terminal(tmp_path: Path) -> None:
    """The interactive menus speak German too, and track B stays a replacement."""

    work, env = _launcher_sandbox(tmp_path)
    rc, out = _run_launcher_on_tty(work, env, "\n\n", args=("--lang", "de"))
    assert rc == 0, out
    assert "Wie sollen die fertigen Anmeldedaten" in out
    assert "Auswahl [Enter = A]: " in out
    assert "How should the finished credentials" not in out
    assert "STATT" in out and "gelöscht" in out, (
        "the German menu must present track B as a replacement that deletes the file"
    )
    assert "zusätzlich" not in out


# Lines that are the same in every language: a menu entry built from data, a
# command and a command line. The two --lang errors print both languages because
# they fire before any language is known.
_LANGUAGE_NEUTRAL = {
    'echo "[login]   ${idx}) ${cand}"',
    'echo "[login]   ssh -L 7900:127.0.0.1:7900 <docker-host>"',
    '[ -n "$_ip" ] && echo "[login]   bash login.sh --ip ${_ip}"',
    'echo "[login] --lang needs a language tag, e.g. --lang de" >&2',
    'echo "[login] --lang braucht ein Sprachkürzel, z. B. --lang de" >&2',
}


def test_every_launcher_line_goes_through_msg() -> None:
    """A new [login] line must be a msg/pick pair, or it would stay English-only."""

    text = (DOCKER_LOGIN / "login.sh").read_text("utf-8")
    plain = {
        line.strip()
        for line in text.splitlines()
        if "[login]" in line
        and not line.lstrip().startswith("#")
        and re.search(r"\b(echo|printf) ['\"]\[login\]", line)
    }
    assert plain == _LANGUAGE_NEUTRAL, (
        "these [login] lines bypass msg/pick and stay English for a German "
        f"user: {sorted(plain - _LANGUAGE_NEUTRAL)}"
    )

    pairs = re.findall(r'msg "([^"]*)" \\\n\s*"([^"]*)"', text)
    assert len(pairs) >= 40, "the msg pairs were not found; did the call form change?"
    same = [en for en, de in pairs if en == de]
    assert not same, f"untranslated msg pairs: {same}"
    one_arg = re.findall(r'^\s*msg "[^"]*"\s*(?:>&2)?\s*$', text, re.M)
    assert not one_arg, f"msg needs two texts (EN, DE): {one_arg}"


# --- login.cmd -----------------------------------------------------------
# There is no Windows here, so login.cmd is pinned by text. The one property
# that CAN be measured against a running program, which tags count as German,
# is compared with the real login.sh below.

_CMD_EN = "if not defined LAUNCHER_DE "
_CMD_DE = "if defined LAUNCHER_DE "

# The two --lang errors print both languages because they fire before any
# language is known; the ssh line is a command line, the same in every language.
_CMD_LANGUAGE_NEUTRAL = {
    "echo [login] --lang needs a language tag, e.g. login.cmd --lang de 1>&2",
    "echo [login] --lang braucht ein Sprachkuerzel, z. B. login.cmd --lang de 1>&2",
    "echo [login]   ssh -L 7900:127.0.0.1:7900 ^<docker-host^>",
}


def _cmd_text() -> str:
    return (DOCKER_LOGIN / "login.cmd").read_text("ascii")


def _cmd_code_lines() -> list[tuple[int, str]]:
    """Executable lines of login.cmd with their index; comments and blanks dropped."""

    out = []
    for index, line in enumerate(_cmd_text().splitlines()):
        stripped = line.strip()
        if not stripped or stripped.lower().startswith(("rem ", "@rem ", "::")):
            continue
        out.append((index, stripped))
    return out


def _split_cmd_message(line: str) -> tuple[str, str, str]:
    """Split ``<conditions> echo|set /p <text> [1>&2]`` into its three parts."""

    match = re.match(r"^(.*?)((?:echo |set /p ).*?)( 1>&2)?$", line)
    assert match, f"not a message line: {line!r}"
    return match.group(1), match.group(2), match.group(3) or ""


def test_every_login_cmd_line_is_an_adjacent_language_pair() -> None:
    """Each [login] line of login.cmd is EN then DE, same conditions, translated.

    The pair is two neighbouring lines rather than a lookup routine, because a
    parenthesised block or a `call` with the text as argument would expand the
    `%VAR%` parts at the wrong time and mangle `^<`, `"` and `)` in the texts.
    Being neighbours is what keeps a translation from drifting away.
    """

    lines = _cmd_code_lines()
    messages = [
        (pos, line)
        for pos, (_, line) in enumerate(lines)
        if "[login]" in line and line not in _CMD_LANGUAGE_NEUTRAL
    ]
    bare = [line for _, line in messages if not line.startswith((_CMD_EN, _CMD_DE))]
    assert not bare, f"these [login] lines stay English for a German user: {bare}"

    pairs = 0
    for pos, line in messages:
        if not line.startswith(_CMD_EN):
            continue
        assert pos + 1 < len(lines), f"English line without a German partner: {line!r}"
        partner = lines[pos + 1][1]
        assert partner.startswith(_CMD_DE), (
            f"the German partner must follow directly: {line!r} / {partner!r}"
        )
        en_cond, en_body, en_redirect = _split_cmd_message(line[len(_CMD_EN) :])
        de_cond, de_body, de_redirect = _split_cmd_message(partner[len(_CMD_DE) :])
        assert en_cond == de_cond, f"pair under different conditions: {line!r}"
        assert en_redirect == de_redirect, f"pair writes to different streams: {line!r}"
        assert en_body.split(" ", 1)[0] == de_body.split(" ", 1)[0], line
        assert en_body != de_body, f"untranslated pair: {line!r}"
        pairs += 1
    german = sum(1 for _, line in messages if line.startswith(_CMD_DE))
    assert german == pairs, "a German line has no English line directly above it"
    assert pairs >= 30, f"only {pairs} pairs found; did the pair form change?"


def test_login_cmd_stays_ascii_and_keeps_the_code_page() -> None:
    """German on Windows is written ae/oe/ue/ss, so no code page has to change.

    An umlaut in this file would need the console code page switched, which
    nobody can verify without a Windows console, and a wrong guess would garble
    exactly the lines meant for German users.
    """

    raw = (DOCKER_LOGIN / "login.cmd").read_bytes()
    raw.decode("ascii")
    executable = "\n".join(line for _, line in _cmd_code_lines()).lower()
    assert "chcp" not in executable, "login.cmd must not switch the code page"
    assert "geloescht" in _cmd_text(), "the German texts are missing altogether"


def test_login_cmd_reads_lang_before_any_output() -> None:
    """--lang and the language must be settled before the parser can print."""

    lines = _cmd_code_lines()

    def first(predicate: str) -> int:
        found = [index for index, line in lines if line.startswith(predicate)]
        assert found, f"login.cmd has no line starting with {predicate!r}"
        return found[0]

    trim = next(index for index, line in lines if "call :trim_trailing_blanks" in line)
    prescan = first("call :prescan_lang %*")
    refusal = first("if errorlevel 1 goto :lang_missing")
    culture = first("if not defined GFMY_LOCALE (")
    decided = first('set "LAUNCHER_DE="')
    parser = first(":parse_args")
    first_output = min(
        index
        for index, line in lines
        if re.search(r"\becho[ .]", line) and line.lower() != "@echo off"
    )

    assert trim < prescan, "trim GFMY_* first, as for every other switch"
    order = [i for i, _ in lines]
    assert order.index(refusal) == order.index(prescan) + 1, (
        "a --lang without a value must be refused right after the scan"
    )
    assert prescan < culture < decided < parser < first_output, (
        "--lang, then the Windows culture, then the launcher language, and all "
        "of that before the parser, which already prints messages"
    )


_GERMAN_PROBES = [
    "de",
    "DE",
    "De-AT",
    "de-DE",
    "de_DE.UTF-8",
    "de.UTF-8",
    "de@euro",
    "dex",
    "deu",
    "en",
    "en-DE",
    "C",
    "POSIX",
    "fr_DE.UTF-8",
]


def _cmd_counts_as_german(value: str) -> bool:
    """Evaluate login.cmd's own detection lines for ``value``."""

    text = _cmd_text()
    exact = re.findall(r'if /i "%_LOC%"=="([^"]+)" set "LAUNCHER_DE=1"', text)
    prefixes = re.findall(
        r'if /i "%_LOC:~0,(\d+)%"=="([^"]+)" set "LAUNCHER_DE=1"', text
    )
    assert exact and prefixes, "login.cmd lost its German detection"
    assert "if not defined _LOC goto :launcher_lang_done" in text
    low = value.lower()
    return low in {e.lower() for e in exact} or any(
        low[: int(n)] == p.lower() for n, p in prefixes
    )


def test_login_cmd_counts_the_same_tags_as_german_as_login_sh(
    tmp_path: Path,
) -> None:
    """Terminal language must not depend on which launcher a user started.

    login.sh decides by a `case` pattern, login.cmd by prefix tests; the real
    login.sh is the reference, since it is the one that can run here.
    """

    differ = []
    for index, value in enumerate(_GERMAN_PROBES):
        sub = tmp_path / f"p{index}"
        sub.mkdir()
        work, env = _launcher_sandbox(sub)
        proc = _run(work, {**env, "GFMY_LOCALE": value}, "--help")
        assert proc.returncode == 0, proc.stderr
        sh_german = proc.stdout.startswith("Aufruf:")
        if sh_german != _cmd_counts_as_german(value):
            differ.append((value, sh_german))
    assert not differ, (
        f"login.sh and login.cmd disagree (value, sh says German): {differ}"
    )


def test_login_cmd_parser_takes_lang_and_refuses_it_empty() -> None:
    text = _cmd_text()
    for needle in (
        'if /i "%~1"=="--lang" goto :parse_lang',
        'if /i "%ARG:~0,7%"=="--lang=" goto :parse_lang_inline',
        'if /i "%~1"=="--lang" goto :prescan_lang_value',
        'set "GFMY_LOCALE=%~2"',
        'set "GFMY_LOCALE=%_PA:~7%"',
    ):
        assert needle in text, f"login.cmd must contain {needle!r}"
    labels = set(re.findall(r"^:([A-Za-z_]+)", text, re.M))
    jumps = set(re.findall(r"goto :([A-Za-z_]+)", text)) - {"eof"}
    assert jumps <= labels, f"login.cmd jumps to undefined labels: {jumps - labels}"
    refusal = text.split("\n:lang_missing\n", 1)[1].split("\n\n", 1)[0]
    assert "--lang needs a language tag" in refusal
    assert "--lang braucht ein Sprachkuerzel" in refusal
    assert refusal.rstrip().endswith("exit /b 2"), "a usage error exits with 2"


def test_login_cmd_help_exists_in_both_languages() -> None:
    text = _cmd_text()
    usage = text.split("\n:print_usage\n", 1)[1]
    assert usage.startswith("if defined LAUNCHER_DE goto :print_usage_de\n")
    english = usage.split("\ngoto :eof", 1)[0]
    german = text.split("\n:print_usage_de\n", 1)[1].split("\ngoto :eof", 1)[0]
    assert "Aufruf: login.cmd" in german and "Usage: login.cmd" in english

    def options(block: str) -> set[str]:
        return set(re.findall(r"^echo   (--[a-z]+)", block, re.M))

    def names(block: str) -> set[str]:
        return set(re.findall(r"^echo   (GFMY_[A-Z_]+)", block, re.M))

    assert (
        options(english) == options(german) == {"--ip", "--track", "--lang", "--help"}
    )
    assert names(english) == names(german)
    assert "set GFMY_LOCALE=C" in german, "the Windows opt-out, in German too"
