# tests/test_docker_login_hardening.py
"""Regression guards for the docker-login helper hardening.

These are text guards (in the spirit of ``test_hacs_validation.py``) that lock in
security/usability fixes for the containerised login helper so they cannot silently
regress. Each guard maps to a concrete Codex review finding on PR #1208:

* Secrets file must stay owner-only ``0600`` and be handed to the host user via an
  ownership handoff, never relaxed to world-readable ``0644`` (findings A + C).
* The launchers must start the container with ``docker compose run`` (interactive,
  stdin attached) rather than ``docker compose up``, or the one-time
  ``Press Enter`` prompt blocks forever (finding B).
* noVNC must stay bound to loopback by default (earlier finding), so the
  fixed-password session is not exposed on the LAN during sign-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOCKER_LOGIN = Path("custom_components/googlefindmy/docker-login")


def _read(name: str) -> str:
    """Return the text of a docker-login helper file."""

    return (DOCKER_LOGIN / name).read_text(encoding="utf-8")


def test_entrypoint_never_makes_secrets_world_readable() -> None:
    """The entrypoint must not relax secrets.json to a group/world-readable mode."""

    entrypoint = _read("entrypoint.sh")
    for forbidden in ("chmod 0644", "chmod 644", "chmod 0640", "chmod 0o644"):
        assert forbidden not in entrypoint, (
            f"entrypoint.sh must not widen secrets.json permissions ({forbidden!r}); "
            "keep owner-only 0600 and hand ownership to the host user instead."
        )


def test_entrypoint_hands_ownership_to_host_user() -> None:
    """The entrypoint must chown the produced secrets to the host UID and keep 0600."""

    entrypoint = _read("entrypoint.sh")
    assert "sudo chown" in entrypoint, (
        "entrypoint.sh must perform the ownership handoff"
    )
    assert "GFMY_HOST_UID" in entrypoint, "entrypoint.sh must honour the host UID"
    assert "chmod 0600" in entrypoint, (
        "entrypoint.sh must keep the produced secrets.json owner-only (0600)"
    )


@pytest.mark.parametrize("launcher", ["login.sh", "login.cmd"])
def test_launcher_uses_compose_run_not_up(launcher: str) -> None:
    """Launchers must use interactive ``compose run`` so the Enter prompt reaches Python."""

    text = _read(launcher)
    assert "docker compose run" in text, (
        f"{launcher} must start the container with `docker compose run` "
        "(stdin attached), not `docker compose up`."
    )
    assert "--rm" in text, (
        f"{launcher} must use --rm so repeated logins do not stack containers"
    )
    assert "docker compose up" not in text, (
        f"{launcher} must not use `docker compose up`: it does not forward stdin, "
        "so the interactive login prompt blocks forever."
    )


def test_launcher_sh_exports_host_ids_without_world_writable_chmod() -> None:
    """login.sh must pass the host UID/GID and must not open ./data to the world."""

    text = _read("login.sh")
    assert "GFMY_HOST_UID" in text and "GFMY_HOST_GID" in text, (
        "login.sh must export the host UID/GID for the ownership handoff"
    )
    assert "chmod 0777" not in text, (
        "login.sh must not make the token directory world-writable; the container "
        "takes ownership of ./data for the run instead."
    )


def test_compose_passes_host_ids_for_handoff() -> None:
    """docker-compose.yml must forward the host UID/GID to the container."""

    compose = _read("docker-compose.yml")
    assert "GFMY_HOST_UID" in compose and "GFMY_HOST_GID" in compose, (
        "docker-compose.yml must forward GFMY_HOST_UID/GFMY_HOST_GID for the handoff"
    )


def test_compose_binds_novnc_to_loopback_by_default() -> None:
    """noVNC must default to a loopback bind, not be exposed on the LAN."""

    compose = _read("docker-compose.yml")
    assert "127.0.0.1" in compose, (
        "docker-compose.yml must bind the fixed-password noVNC port to loopback by default"
    )
