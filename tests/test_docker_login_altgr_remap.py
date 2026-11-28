# tests/test_docker_login_altgr_remap.py
"""Keyboard remap written by ``docker-login/entrypoint.sh`` (AltGr characters).

The keyboard block is EXECUTED here, not grepped: what x11vnc gets is the
``~/.x11vncrc`` the block writes, so the assertions read that file. The block
is taken through ``_extract_shell_block`` (see ``tests/AGENTS.md``, "Shell
fragment extraction"), which raises instead of handing over a larger stretch.

Why ``Alt_R`` is on the list: on the host, AltGr+Q arrives at the VNC level as
``Alt_R`` held down plus the finished ``at`` keysym. Dropping only
``ISO_Level3_Shift`` and ``Mode_switch`` left ``Alt_R`` injected around the
character, and Chrome read Alt+@ as a shortcut instead of the character. The
price is the real right Alt key inside the container, which no login needs.

Why the keypad digits are on the same line: noVNC sends a keypad digit as its
own keysym (``KP_0`` and so on), and with Num Lock off in Xvfb x11vnc delivered
the key's navigation level (``KP_Insert``), so no digit appeared. Mapping
``KP_n`` to ``n`` keeps the keypad navigation keys (``KP_End`` and friends,
sent when Num Lock is off on the host) untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_docker_login_hardening import (
    _extract_shell_block,
    _read,
    _run_shell_fragment,
)

_DROPPED = {"ISO_Level3_Shift", "Mode_switch", "Alt_R"}


def _keyboard_block() -> str:
    """Return the ``GFMY_KEYBOARD_FIX`` block, from its banner through its ``fi``."""

    return _extract_shell_block(
        _read("entrypoint.sh"),
        banner="# --- Keyboard: make AltGr characters reach Chrome",
        opener='if [ "${GFMY_KEYBOARD_FIX:-1}" = "1" ]; then',
        must_contain=(".x11vncrc", "remap ", "GFMY_KEYBOARD_FIX=0"),
        max_lines=80,
        label="the keyboard remap",
    )


def _run_block(tmp_path: Path, env: dict[str, str]) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    result = _run_shell_fragment(
        _keyboard_block(), ":", bin_dir, {"HOME": str(home), **env}
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith("REACHED_END"), result.stdout
    return home / ".x11vncrc"


def _remap_pairs(rc_file: Path) -> dict[str, str]:
    """Return the single ``remap`` line of ``rc_file`` as source -> target."""

    lines = [
        line
        for line in rc_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("remap ")
    ]
    assert len(lines) == 1, lines
    pairs = [p.partition("-") for p in lines[0].removeprefix("remap ").split(",")]
    sources = [source for source, _, _ in pairs]
    assert len(sources) == len(set(sources)), sources
    return {source: target for source, _, target in pairs}


def _remapped_to_nosymbol(rc_file: Path) -> set[str]:
    return {
        source
        for source, target in _remap_pairs(rc_file).items()
        if target == "NoSymbol"
    }


def _remapped_to_a_keysym(rc_file: Path) -> dict[str, str]:
    return {
        source: target
        for source, target in _remap_pairs(rc_file).items()
        if target != "NoSymbol"
    }


_KEYPAD_DIGITS = {f"KP_{digit}": str(digit) for digit in range(10)}


def test_default_run_drops_right_alt_with_the_level3_modifiers(
    tmp_path: Path,
) -> None:
    """Without opt-out, ``Alt_R`` is remapped to NoSymbol next to the level-3 keys."""

    rc_file = _run_block(tmp_path, {})
    assert _remapped_to_nosymbol(rc_file) == _DROPPED
    assert "xkb" in rc_file.read_text(encoding="utf-8").splitlines()


def test_default_run_maps_every_keypad_digit_to_its_digit(tmp_path: Path) -> None:
    """``KP_0`` .. ``KP_9`` become ``0`` .. ``9``, and nothing else is remapped."""

    rc_file = _run_block(tmp_path, {})
    assert _remapped_to_a_keysym(rc_file) == _KEYPAD_DIGITS


def test_opt_out_leaves_no_remap_behind(tmp_path: Path) -> None:
    """``GFMY_KEYBOARD_FIX=0`` removes a file written by an earlier run."""

    home = tmp_path / "home"
    home.mkdir()
    (home / ".x11vncrc").write_text("remap Alt_R-NoSymbol\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    result = _run_shell_fragment(
        _keyboard_block(),
        ":",
        bin_dir,
        {"HOME": str(home), "GFMY_KEYBOARD_FIX": "0"},
    )
    assert result.returncode == 0, result.stderr
    assert not (home / ".x11vncrc").exists()


def _readme_altgr_section() -> str:
    """Return the README section on AltGr characters, heading to next heading."""

    readme = _read("README.md")
    heading = '### Typing "@" and other AltGr characters'
    assert heading in readme, (
        "README heading renamed; update this test and the troubleshooting "
        "anchor #typing--and-other-altgr-characters"
    )
    start = readme.index(heading)
    match = re.compile(r"\n#{1,3} ").search(readme, start + len(heading))
    return readme[start : match.start() if match else len(readme)]


def test_readme_names_every_dropped_key_and_the_price(tmp_path: Path) -> None:
    """The README names each keysym the block drops, the lost key and the opt-out.

    The dropped set comes from the executed block, so a key added to the remap
    without a README sentence turns this test red.
    """

    section = _readme_altgr_section()
    for keysym in sorted(_remapped_to_nosymbol(_run_block(tmp_path, {}))):
        assert f"`{keysym}`" in section, keysym
    assert "The price is the right Alt key itself" in section
    assert "it does nothing" in section
    assert "`GFMY_KEYBOARD_FIX=0`" in section


def test_readme_names_the_keypad_digit_mapping(tmp_path: Path) -> None:
    """The README names the keypad mapping the block writes and what stays as it was.

    The range comes from the executed block: a keypad key added to or dropped
    from the remap without a README change turns this test red.
    """

    mapped = _remapped_to_a_keysym(_run_block(tmp_path, {}))
    assert mapped == _KEYPAD_DIGITS
    first, last = min(mapped), max(mapped)
    section = " ".join(_readme_altgr_section().split())
    assert f"(`{first}` to `{last}`)" in section
    assert f"`{mapped[first]}` to `{mapped[last]}`" in section
    assert "Num Lock is off inside the container" in section
    assert "the keypad keys still move the cursor as before" in section
    assert "switches this mapping off" in section
