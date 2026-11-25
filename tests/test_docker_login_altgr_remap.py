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
"""

from __future__ import annotations

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


def _remapped_to_nosymbol(rc_file: Path) -> set[str]:
    lines = [
        line
        for line in rc_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("remap ")
    ]
    assert len(lines) == 1, lines
    pairs = lines[0].removeprefix("remap ").split(",")
    return {
        source
        for source, _, target in (p.partition("-") for p in pairs)
        if target == "NoSymbol"
    }


def test_default_run_drops_right_alt_with_the_level3_modifiers(
    tmp_path: Path,
) -> None:
    """Without opt-out, ``Alt_R`` is remapped to NoSymbol next to the level-3 keys."""

    rc_file = _run_block(tmp_path, {})
    assert _remapped_to_nosymbol(rc_file) == _DROPPED
    assert "xkb" in rc_file.read_text(encoding="utf-8").splitlines()


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
