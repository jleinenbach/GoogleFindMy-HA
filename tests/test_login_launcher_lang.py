# tests/test_login_launcher_lang.py
"""The launcher speaks the language of ``GFMY_LOCALE``, and ``--lang`` sets it (L-11).

``login.sh`` prints its own terminal lines in English by default and in German
for a ``de*`` tag. The tag comes from ``--lang`` (the short name), else from an
inherited ``GFMY_LOCALE``, else from the shell locale, which is the same value
the container hands to Chrome. Terminal and sign-in page therefore cannot
disagree. The noVNC viewer is out of scope on purpose: it follows the browser
language (decision NA-2), which the README states.

The behavioural tests run the REAL launcher against a stub ``docker``. The text
guard at the end pins the one property a behavioural test cannot reach in
full: every ``[login]`` line goes through ``msg``/``pick``, so a new message
cannot quietly stay English-only.
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
