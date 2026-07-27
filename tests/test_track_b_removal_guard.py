# tests/test_track_b_removal_guard.py
"""Repo-wide guard against the return of the removed token-endpoint handoff.

The failure class this pins is not "a file came back" but "the surface came
back": a handoff that hands Google credentials to Home Assistant over an
unencrypted, host-published port. PR #1218 removed it on both sides (the login
container and the config flow) after weighing the alternatives; the transport
could only be made safe at a cost in user steps that exceeded simply pasting the
bundle.

A guard listing the files it once lived in would say nothing about a
reintroduction somewhere else, so this one scans the whole versioned tree for
the *vocabulary* of that surface instead: the port, the switches, the overlay
file, the server module, the client primitives, the error keys, the flow steps
and the form fields. Anything that carries them is either the surface returning
or a stale mention of it, and both are findings.

**Honest about its reach.** This is a vocabulary sweep, not a semantic one: a
reintroduction that renames everything (another port, another switch, another
module) passes it. What it does catch is the realistic regression -- a revert, a
cherry-pick, a copied snippet, a doc paragraph that outlived its subject -- and
it catches those anywhere in the tree rather than only where they used to be. A
deliberate reimplementation under new names is a design decision that belongs in
review, not in a grep.

Two deliberate non-patterns:

* ``container_login`` alone is NOT searched. ``chrome_driver._is_container_login``
  reads ``GOOGLEFINDMY_CONTAINER_LOGIN`` and belongs to the login container's own
  startup, which survives; searching the bare word would flag it on every run.
* the track letter is matched with word boundaries (not a bare ``track``), so
  prose like "let the coordinator track consecutive failures" stays clean.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The vocabulary of the removed surface. Assembled from fragments rather than
# spelled out, so this file does not match itself and no allowlist is needed to
# excuse it (see the module docstring for why an allowlist is the wrong tool
# here).
_REMOVED_TRACK_LETTER = chr(ord("a") + 2)
_TOKEN_PORT = str(7900 + 1)
_ONECLICK = "ONE" + "CLICK"
_PATTERNS: tuple[str, ...] = (
    # The published port, as a standalone number: a bare substring match would
    # flag any longer number that happens to contain it.
    rf"(?<!\d){_TOKEN_PORT}(?!\d)",
    f"GFMY_{_ONECLICK}",
    r"docker-compose\." + _ONECLICK.lower(),
    "[Oo]ne-click",
    "ONE-CLICK",
    rf"\b[Tt]rack [{_REMOVED_TRACK_LETTER.upper()}{_REMOVED_TRACK_LETTER}]\b",
    # Form fields and flow steps.
    "pairing_code",
    "container_host",
    "container_port",
    "async_step_container",
    # Constants and modules.
    "CONTAINER_TOKEN_",
    "CONTAINER_NONCE_",
    "CONTAINER_FETCH_TIMEOUT",
    "CONTAINER_MAX_RESPONSE_BYTES",
    "token_server",
    # Client primitives and their vocabulary.
    "fetch_secrets_from_container",
    "ack_consumed",
    "delete_token",
    "pairing nonce",
    # Error keys of the removed path.
    "container_unreachable",
    "container_timeout",
    "container_auth_failed",
    "container_code_used",
    "container_code_too_short",
    "container_login_failed",
)
_SWEEP = re.compile("|".join(_PATTERNS))

# Extensions worth scanning: everything the removed surface ever appeared in.
# An allowlist, not a denylist, so a new binary format cannot silently widen the
# scan into unreadable files.
_SCANNED_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".rst",
        ".txt",
        ".sh",
        ".cmd",
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".cfg",
        ".ini",
        ".env",
        ".js",
        ".html",
    }
)
_SCANNED_NAMES = frozenset({"Dockerfile", "Makefile"})

# Directories and files that are not source of this repository (vendored
# environments, caches) or that carry random hex which can contain the port
# digits by accident.
_EXCLUDED_DIRS = frozenset({".git", ".venv", "__pycache__", ".mypy_cache"})
_EXCLUDED_FILES = frozenset({"poetry.lock", "package-lock.json"})

# This guard has to name what it forbids, so it is the one file the sweep may
# not read. Excluded by its resolved PATH, not by its name, so a same-named file
# elsewhere in the tree is still scanned.
_SELF = Path(__file__).resolve()


def _scannable_files() -> list[Path]:
    """Return every file of this repository the sweep applies to."""

    found: list[Path] = []
    for path in _REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _EXCLUDED_DIRS for part in path.relative_to(_REPO_ROOT).parts):
            continue
        if path.name in _EXCLUDED_FILES or path.resolve() == _SELF:
            continue
        if path.suffix not in _SCANNED_SUFFIXES and path.name not in _SCANNED_NAMES:
            continue
        found.append(path)
    return sorted(found)


def test_no_token_endpoint_surface_remains_in_the_repo() -> None:
    """No file may carry the vocabulary of the removed handoff.

    Every hit is reported, not just the first: a fix pass should be able to
    clear the finding in one go instead of rediscovering it line by line.
    """

    hits: list[str] = []
    for path in _scannable_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - defensive, allowlisted suffixes
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _SWEEP.search(line):
                rel = path.relative_to(_REPO_ROOT)
                hits.append(f"{rel}:{number}: {line.strip()}")

    assert not hits, (
        "the one-shot token-endpoint handoff was removed for good in PR #1218 "
        "(unencrypted transport of the credential bundle); these lines bring its "
        "surface or a stale mention of it back:\n" + "\n".join(hits)
    )


def test_the_removed_files_are_gone() -> None:
    """A revert of the deletions must not pass unnoticed.

    The sweep above catches the vocabulary, which a restored file would carry
    too -- but only if its content still spells it. Pinning the paths as well
    means a revert fails on the paths even if someone renames the identifiers.
    """

    removed = (
        "custom_components/googlefindmy/container_login.py",
        "custom_components/googlefindmy/docker-login/token_server.py",
        "custom_components/googlefindmy/docker-login/docker-compose.oneclick.yml",
        "tests/test_config_flow_container_login.py",
        "tests/test_token_server.py",
    )
    present = [rel for rel in removed if (_REPO_ROOT / rel).exists()]
    assert not present, (
        f"these files were deleted with the handoff in PR #1218: {present}"
    )
