# tests/test_docker_login_hardening.py
"""Regression guards for the docker-login helper hardening.

Most guards here are text guards (in the spirit of ``test_hacs_validation.py``)
that lock in security/usability fixes for the containerised login helper so they
cannot silently regress; the final guard
(``test_backgrounded_login_child_behaviourally_keeps_stdin_and_default_sigint``)
is a BEHAVIOURAL test that runs the real backgrounding construct from
``entrypoint.sh`` and asserts stdin/SIGINT at runtime, so the launch fix is proven
by behaviour rather than by matching script text. Each guard maps to a concrete
Codex review finding on PR #1208:

* Secrets file must stay owner-only ``0600`` and be handed to the host user via an
  ownership handoff, never relaxed to world-readable ``0644`` (findings A + C).
* The launchers must start the container with ``docker compose run`` (interactive,
  stdin attached) rather than ``docker compose up``, or the account-e-mail prompt
  blocks forever (finding B).
* noVNC must stay bound to loopback by default (earlier finding), so the
  fixed-password session is not exposed on the LAN during sign-in.
* A context-level ``.dockerignore`` must be an ALLOWLIST (ignore the whole
  build context, re-include only the two paths the Dockerfile COPYs) so that
  EVERY secret location -- ``docker-login/data`` AND the legacy
  ``Auth/secrets.json`` AND any future token/cache path -- stays out of the
  ``--build`` context, not just the one path a denylist happens to name. A bare
  ``!docker-login`` re-includes the WHOLE directory, so it is re-excluded again
  (``docker-login/*``) down to just the one re-included file
  (``!docker-login/entrypoint.sh``); a file dropped directly under
  ``docker-login/`` must NOT reach the context (Codex P2 on PR #1210).
* The QNAP "Container Station" README guidance must NOT present importing the
  compose file as an *application* (``docker compose up`` semantics, no stdin) as
  a first-login path: the ``input("Enter your Google account email:")`` prompt can
  never be answered without a terminal, so the login must go through the stdin-attaching
  ``docker compose run`` (SSH) path (Codex P2 on PR #1210).
* The ownership handoff must run from an ``EXIT`` trap, so a nonzero/interrupted
  ``main.py`` run still returns the produced secrets.json to the host user.
* The CLI must run as a BACKGROUND child that the entrypoint waits on, and the
  signal traps must FORWARD SIGTERM/SIGINT to it. A foreground child makes bash
  defer the trap until the child exits, so ``docker stop`` escalates to SIGKILL and
  the EXIT cleanup never runs (finding: forward termination signals to the child).
* Backgrounding that child must PRESERVE its terminal stdin (``<&0`` at the async
  boundary) and RESET SIGINT/SIGQUIT to default (``trap - INT QUIT`` before exec):
  a non-interactive script otherwise gives an async child ``/dev/null`` stdin (the
  ``input()`` login prompt hits EOF) and hard-ignores SIGINT (Python drops Ctrl-C),
  both regressions of the backgrounding fix above.
* The one-click token port (7901) must be an OPT-IN publish. The launchers always
  pass ``--service-ports``, so anything declared in ``docker-compose.yml`` is
  published on EVERY run; an unconditional 7901 entry made the container fail to
  start on a host where that port was already taken, which also broke the file
  handoff (``./data``) and the ``GFMY_CLEARTEXT=1`` output, neither of which uses
  the port. The publish therefore lives in the opt-in overlay
  ``docker-compose.oneclick.yml`` that the launchers add only for
  ``GFMY_ONECLICK=1`` (Codex P2 on PR #1211). When it IS published, the host part
  defaults to loopback and is widened only by an explicit ``GFMY_ONECLICK_BIND``
  (the counterpart of ``GFMY_NOVNC_BIND`` for noVNC 7900, with two extra rules
  the launchers enforce: a wildcard is refused, and a LAN value is warned about
  because this endpoint serves clear-text tokens).
* ``GFMY_ONECLICK`` and ``GFMY_CLEARTEXT`` are documented (compose + README) as
  INDEPENDENT switches, but the clear-text track hung off the one-click branch as
  an ``elif``, so with both set it was never evaluated -- in the documented lockout
  case the token server deliberately KEEPS ``secrets.json``, yet the requested
  clear-text fallback stayed silent. The tracks are now two separate ``if``s, each
  re-testing ``-f "${_secrets_path}"`` so an acked/TTL-consumed (deleted) bundle
  still prints nothing (Codex P2 on PR #1211).
* The One-Click token server ran as a FOREGROUND child. Bash is PID 1 (exec-form
  ``ENTRYPOINT``) and defers its TERM/INT traps until a foreground child returns,
  which for this server can be its full 300 s TTL -- so ``docker stop`` escalated
  to SIGKILL after the grace period and the EXIT cleanup (``/data`` ownership
  handoff + supervisor shutdown) never ran, leaving the 0600 bundle owned by the
  container UID (Codex P2 on PR #1211). Both long-running children now go through
  one tracked slot and one wait helper. Backgrounding alone is not enough: the
  child then dies on the relayed signal and its ``wait`` returns NORMALLY, so a
  shutdown check after the wait stops the run instead of falling through into the
  clear-text track and dumping the bundle into the container log.
* The login image installs ``setuptools`` alongside ``requirements.txt`` in its
  build step. undetected-chromedriver 3.5.5 (the current PyPI top) still imports
  the stdlib ``distutils`` that Python 3.12+ removed (PEP 632); the
  ``selenium/standalone-chrome:latest`` base image is on Python 3.14, so without
  ``setuptools`` re-providing ``distutils`` via its ``_distutils_hack`` the bare
  import fails (``ModuleNotFoundError: distutils``) and Chrome never starts.
  Dropping ``setuptools`` from the install step would restore that failure while
  every other guard here stays green (Codex P1 on PR #1215).
"""

from __future__ import annotations

import os
import pty
import re
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

DOCKER_LOGIN = Path("custom_components/googlefindmy/docker-login")
# Build context root for the login image (docker-compose.yml `build.context: ..`).
BUILD_CONTEXT = DOCKER_LOGIN.parent
LOGIN_SERVICE = "googlefindmy-login"
# Opt-in overlay carrying the one-click token-port publish (Codex P2, PR #1211).
ONECLICK_COMPOSE = "docker-compose.oneclick.yml"


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
    """Launchers must use interactive ``compose run`` so stdin reaches Python."""

    text = _read(launcher)
    # `docker compose [-f ...] run`: the launchers now select their compose files
    # explicitly (base always, the one-click overlay only on opt-in), so the
    # invocation carries flags between `compose` and `run`. The subcommand must
    # still be `run` -- match it as a word, not the bare literal string.
    assert re.search(r"docker compose\s+(?:\S+\s+)*run\b", text), (
        f"{launcher} must start the container with `docker compose [-f ...] run` "
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


def _compose_service_ports(compose_file: str) -> list[str]:
    """Return the login service's declared port publishes from ``compose_file``.

    Parsed from YAML (not grepped) so the extensive *comments* about port 7901 in
    both files cannot make a guard pass or fail by accident: only real ``ports:``
    entries count.
    """

    parsed = yaml.safe_load(_read(compose_file))
    service = parsed["services"][LOGIN_SERVICE]
    return [str(entry) for entry in service.get("ports", [])]


def test_base_compose_does_not_publish_the_oneclick_token_port() -> None:
    """The base compose must publish noVNC only, never the one-click token port.

    Both launchers start the service with ``--service-ports``, so every entry in
    the base file's ``ports:`` list is published on EVERY run. With 7901 declared
    unconditionally, a host that already used that port (another service, a
    leftover container) made the container fail to start altogether -- taking down
    the two handoff tracks that never touch the port: the file handoff through
    ``./data`` and the ``GFMY_CLEARTEXT=1`` terminal output. Compose cannot drop a
    single ``ports:`` entry conditionally, so the publish moved into the opt-in
    overlay asserted below (Codex P2 on PR #1211).
    """

    ports = _compose_service_ports("docker-compose.yml")

    assert not [p for p in ports if "7901" in p], (
        "docker-compose.yml must NOT declare a publish for the one-click token "
        f"port 7901 (found {ports!r}); it belongs in {ONECLICK_COMPOSE}, which the "
        "launchers add only for GFMY_ONECLICK=1. Otherwise a busy host port 7901 "
        "blocks the file-handoff and clear-text tracks that do not need it."
    )
    # noVNC stays published in every track (it is how you perform the login).
    assert any("7900" in p for p in ports), (
        "docker-compose.yml must keep publishing the noVNC viewer port 7900 for "
        f"all tracks (found {ports!r})."
    )


def test_oneclick_overlay_publishes_token_port_with_a_loopback_default() -> None:
    """The overlay must publish 7901 as ``${GFMY_ONECLICK_BIND:-127.0.0.1}:7901:7901``.

    The endpoint serves the freshly minted tokens in the clear, so the host-side
    publish IS the security boundary (the in-container bind is ``0.0.0.0`` on
    purpose, because Docker's bridge DNATs the published port onto eth0 rather
    than onto container loopback).

    Until the bind became configurable this was a literal ``127.0.0.1``. That pin
    made the one-click track usable only where the file handoff already works --
    a Home Assistant in its own bridge container, or on another machine, cannot
    reach the Docker host's loopback -- so the host part is now variable-driven,
    exactly like noVNC's ``GFMY_NOVNC_BIND``. What must NOT change: the DEFAULT is
    loopback, so an unset variable can never widen anything, and neither a
    wildcard publish (``0.0.0.0:7901:7901``) nor a bare ``7901:7901`` may appear.
    The launchers add the checks Compose cannot express (wildcard refused,
    clear-text warning); see the behavioural tests further down.
    """

    overlay = DOCKER_LOGIN / ONECLICK_COMPOSE
    assert overlay.is_file(), (
        f"{ONECLICK_COMPOSE} must exist next to docker-compose.yml: it carries the "
        "opt-in one-click token-port publish that the launchers add on demand."
    )

    ports = _compose_service_ports(ONECLICK_COMPOSE)
    token_ports = [p for p in ports if "7901" in p]
    assert token_ports == ["${GFMY_ONECLICK_BIND:-127.0.0.1}:7901:7901"], (
        f"{ONECLICK_COMPOSE} must publish the token port exactly as "
        f"'${{GFMY_ONECLICK_BIND:-127.0.0.1}}:7901:7901' (found {ports!r}): one "
        "variable, one loopback default. A second spelling, a missing default or "
        "a non-loopback default would widen the publish without anyone asking."
    )
    # The overlay must not silently take over other ports (noVNC is merged in from
    # the base file, including its GFMY_NOVNC_BIND behaviour).
    assert ports == token_ports, (
        f"{ONECLICK_COMPOSE} must only add the token port; noVNC's publish (and its "
        f"GFMY_NOVNC_BIND handling) stays in docker-compose.yml. Found {ports!r}."
    )

    overlay_yaml = yaml.safe_load(_read(ONECLICK_COMPOSE))
    assert "network_mode" not in overlay_yaml["services"][LOGIN_SERVICE], (
        f"{ONECLICK_COMPOSE} must not set network_mode: `host` removes the bridge "
        "and the publish indirection, so the container's 0.0.0.0 bind would land on "
        "the host's LAN interfaces."
    )


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", ONECLICK_COMPOSE])
def test_token_port_has_exactly_one_bind_knob_defaulting_to_loopback(
    compose_file: str,
) -> None:
    """A LAN bind for 7901 must stay opt-in, single-spelled and loopback by default.

    The token port hands out credentials in the clear, so widening it is allowed
    but must always be somebody's explicit decision: exactly ONE variable, always
    with a loopback default, and never a wildcard or a bare mapping (both of which
    publish on every interface without naming an address).

    This is the successor of the older "no LAN opt-in at all" rule. That rule was
    dropped deliberately -- it left the one-click handoff reachable only from the
    machine where the file handoff already works -- but everything it protected
    against (a silent widening) is still asserted here.
    """

    allowed = "${GFMY_ONECLICK_BIND:-127.0.0.1}:7901:7901"
    for entry in _compose_service_ports(compose_file):
        if "7901" not in entry:
            continue
        assert entry == allowed, (
            f"{compose_file} publishes the token port as {entry!r}. It must be "
            f"exactly {allowed!r}: no wildcard, no bare mapping, no second variable "
            "name, and never a default other than loopback."
        )

    text = _read(compose_file)
    assert "0.0.0.0:7901" not in text, (
        f"{compose_file} must never bind the token port to all interfaces."
    )
    # A bare `7901:7901` (optionally quoted, no host-IP prefix) publishes on
    # 0.0.0.0. The loopback form is preceded by a dot-separated IP, so it does not
    # match this pattern.
    assert re.search(r'(^|[\s"\'])7901:7901\b', text) is None, (
        f"{compose_file} must not publish the token port without an explicit "
        "127.0.0.1 host prefix; a bare mapping binds all interfaces."
    )


@pytest.mark.parametrize("launcher", ["login.sh", "login.cmd"])
def test_launcher_selects_base_compose_explicitly(launcher: str) -> None:
    """Both launchers must name the base compose file when they select files.

    Once any ``-f`` is passed, Compose stops auto-discovering, so the base file has
    to be listed too; otherwise the overlay alone would be an incomplete project.
    """

    text = _read(launcher)
    assert "-f docker-compose.yml" in text, (
        f"{launcher} must pass `-f docker-compose.yml` explicitly, because adding "
        "the one-click overlay with -f disables Compose's implicit file discovery."
    )


@pytest.mark.parametrize("launcher", ["login.sh", "login.cmd"])
def test_launcher_adds_oneclick_overlay_only_behind_the_oneclick_gate(
    launcher: str,
) -> None:
    """The overlay (and thus the 7901 publish) may only be added for GFMY_ONECLICK=1.

    If a launcher pulled the overlay in unconditionally the fix would be undone:
    the file-handoff and clear-text tracks would again fail to start on a host
    where port 7901 is taken.
    """

    text = _read(launcher)
    assert ONECLICK_COMPOSE in text, (
        f"{launcher} must be able to add {ONECLICK_COMPOSE} for the one-click track."
    )

    # Every line that actually passes the overlay to Compose must sit behind an
    # explicit GFMY_ONECLICK check (same line for cmd, enclosing `if` for bash).
    adding_lines = [
        line
        for line in text.splitlines()
        if f"-f {ONECLICK_COMPOSE}" in line
        and not line.lstrip().startswith(("#", "rem ", "REM "))
    ]
    assert adding_lines, (
        f"{launcher} must contain an executable line adding `-f {ONECLICK_COMPOSE}`"
    )

    if launcher == "login.cmd":
        for line in adding_lines:
            assert 'if "%GFMY_ONECLICK%"=="1"' in line, (
                "login.cmd must gate the overlay on the same line: "
                f"found an unguarded {line!r}"
            )
        return

    # bash: the add must live inside the `if [ "${GFMY_ONECLICK:-}" = "1" ]` block.
    lines = text.splitlines()
    add = next(i for i, line in enumerate(lines) if line in adding_lines)
    # The ENCLOSING gate is the last one before the add, not the first one in the
    # file: the same `= "1"` idiom is now also used earlier, where the token-port
    # bind is derived for track B. Anchoring on the first occurrence would measure
    # that unrelated block instead. The guard keeps its teeth, because an add
    # moved out of its gate still lands after the matching `fi`.
    gate = max(
        i for i, line in enumerate(lines[:add]) if '"${GFMY_ONECLICK:-}" = "1"' in line
    )
    closing = next(
        i for i, line in enumerate(lines[gate:], gate) if line.strip() == "fi"
    )
    assert gate < add < closing, (
        "login.sh must add the one-click overlay INSIDE the GFMY_ONECLICK gate "
        f"(gate at line {gate + 1}, add at line {add + 1}, fi at line {closing + 1})."
    )


@pytest.mark.parametrize(
    ("oneclick", "expect_overlay"),
    [(None, False), ("", False), ("0", False), ("1", True)],
)
def test_login_sh_behaviourally_selects_compose_files(
    tmp_path: Path, oneclick: str | None, expect_overlay: bool
) -> None:
    """Behavioural proof: run the REAL login.sh against a stub ``docker``.

    Text guards can be fooled by a stale string elsewhere in the file; this test
    executes ``login.sh`` with a fake ``docker`` on ``PATH`` that only echoes its
    arguments, and asserts the ACTUAL command line. It therefore proves the
    property that matters: without ``GFMY_ONECLICK=1`` nothing pulls in the 7901
    publish, so a busy host port cannot block the file-handoff/clear-text tracks;
    with it, the overlay is selected. The launcher's gate mirrors entrypoint.sh,
    which also treats only the literal ``1`` as "on".
    """

    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - CI images all ship bash
        pytest.skip("bash is required to run the launcher")

    work = tmp_path / "docker-login"
    work.mkdir()
    for name in ("login.sh", "docker-compose.yml", ONECLICK_COMPOSE):
        shutil.copy(DOCKER_LOGIN / name, work / name)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text('#!/usr/bin/env bash\nprintf "DOCKER_ARGS: %s\\n" "$*"\n', "utf-8")
    stub.chmod(0o755)

    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    if oneclick is not None:
        env["GFMY_ONECLICK"] = oneclick

    proc = subprocess.run(
        [bash, str(work / "login.sh")],
        capture_output=True,
        timeout=60,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    argline = next(
        line
        for line in proc.stdout.decode().splitlines()
        if line.startswith("DOCKER_ARGS: ")
    )

    assert "-f docker-compose.yml" in argline, (
        f"login.sh must always select the base compose file; got {argline!r}"
    )
    assert "--service-ports" in argline and " run " in argline
    if expect_overlay:
        assert f"-f {ONECLICK_COMPOSE}" in argline, (
            f"GFMY_ONECLICK={oneclick!r} must pull in the one-click overlay; "
            f"got {argline!r}"
        )
    else:
        assert ONECLICK_COMPOSE not in argline, (
            f"GFMY_ONECLICK={oneclick!r} must NOT publish the token port: the "
            f"overlay may not be selected. got {argline!r}"
        )
    assert "How should the finished credentials reach" not in proc.stderr.decode(), (
        "a non-interactive run must never print the handoff menu: CI, a pipe and "
        "`login.sh < /dev/null` have to behave exactly as they did before it existed."
    )


def _launcher_sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Copy the launcher next to its compose files and stub out ``docker``.

    The stub echoes the compose invocation AND the token-endpoint bind that
    ``login.sh`` exported for it. That second line is what makes the bind testable
    without a Docker daemon: the overlay publishes
    ``${GFMY_ONECLICK_BIND:-127.0.0.1}:7901:7901``, so the value Compose would
    interpolate is exactly the value this child process sees.
    """

    work = tmp_path / "docker-login"
    work.mkdir()
    for name in ("login.sh", "docker-compose.yml", ONECLICK_COMPOSE):
        shutil.copy(DOCKER_LOGIN / name, work / name)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf "DOCKER_ARGS: %s\\n" "$*"\n'
        'printf "ONECLICK_BIND: %s\\n" "${GFMY_ONECLICK_BIND:-<unset>}"\n',
        "utf-8",
    )
    stub.chmod(0o755)
    return work, {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}


def _run_launcher_on_tty(
    work: Path, env: dict[str, str], answers: str, timeout: float = 60.0
) -> tuple[int, str]:
    """Run ``login.sh`` with a real terminal on stdin and feed it ``answers``.

    A pty is the only way to exercise the interactive branches at all: both menus
    are gated on ``[ -t 0 ]``, so a pipe silently takes the non-interactive path
    and would make an "the menu did the right thing" assertion vacuously true.
    All answers are written up front (the shell buffers them) and stdout/stderr
    share the pty, so the returned text is what the operator would have seen.
    """

    bash = shutil.which("bash")
    assert bash is not None, "bash is required to run the launcher"

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-local stub PATH
            [bash, str(work / "login.sh")],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        slave = -1
        os.write(master, answers.encode())

        chunks: list[bytes] = []
        selector = selectors.DefaultSelector()
        selector.register(master, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not selector.select(0.5):
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 65536)
            except OSError:  # the child closed the pty: normal end of run
                break
            if not data:
                break
            chunks.append(data)
        selector.close()
        try:
            returncode = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            raise
    finally:
        if slave != -1:
            os.close(slave)
        os.close(master)
    return returncode, b"".join(chunks).decode("utf-8", "replace")


def test_track_menu_defaults_to_the_file_handoff_on_a_bare_enter(
    tmp_path: Path,
) -> None:
    """Enter, Enter must keep the historical behaviour: no overlay, no 7901.

    Track A is the only handoff that needs no port, no network and no shared
    secret, and it runs on every launch anyway. Making it the default answer is
    what keeps the new question from changing anything for the people who just
    press Enter -- and it is the property that silently breaks if the menu ever
    grows a "no default" or reorders its options.
    """

    work, env = _launcher_sandbox(tmp_path)
    # Two answers: the noVNC address menu, then the handoff menu.
    returncode, out = _run_launcher_on_tty(work, env, "\n\n")

    assert returncode == 0, out
    assert "How should the finished credentials reach" in out, (
        "an interactive run must ASK; that is the whole point of the change."
    )
    argline = next(line for line in out.splitlines() if "DOCKER_ARGS: " in line)
    assert ONECLICK_COMPOSE not in argline, (
        f"a bare Enter must select track A, so no 7901 publish; got {argline!r}"
    )
    assert "GFMY_CLEARTEXT" not in out or "cleartext" not in argline.lower()


def test_track_menu_answer_b_publishes_7901_on_the_novnc_address(
    tmp_path: Path,
) -> None:
    """Answering B must add the overlay AND bind 7901 to a reachable address.

    Both halves matter and neither is enough alone: without the overlay there is
    no port, and without a non-loopback bind the port exists but a Home Assistant
    outside this host's network namespace still cannot reach it -- which was the
    original defect. The address menu answer (192.0.2.10) is offered as the
    default for the token endpoint, so a bare Enter accepts it.
    """

    work, env = _launcher_sandbox(tmp_path)
    # noVNC address, then track B, then accept the offered token-endpoint address.
    returncode, out = _run_launcher_on_tty(work, env, "192.0.2.10\nb\n\n")

    assert returncode == 0, out
    argline = next(line for line in out.splitlines() if "DOCKER_ARGS: " in line)
    assert f"-f {ONECLICK_COMPOSE}" in argline, (
        f"answer B must pull in the one-click overlay; got {argline!r}"
    )
    bindline = next(line for line in out.splitlines() if "ONECLICK_BIND: " in line)
    assert "192.0.2.10" in bindline, (
        "answer B must carry the address the operator just confirmed into "
        f"GFMY_ONECLICK_BIND, so the overlay publishes there; got {bindline!r}"
    )
    assert "UNENCRYPTED" in out, (
        "a non-loopback token endpoint must say out loud that it ships the "
        "credentials in clear text; that warning is the whole mitigation."
    )


def test_preset_oneclick_env_skips_the_menu_and_keeps_loopback(
    tmp_path: Path,
) -> None:
    """``GFMY_ONECLICK=1`` must behave exactly as it did before the menu existed.

    This is the precedence rule: anything the operator already decided is not
    asked again. It is also the compatibility promise for every script and every
    README line that predates the menu -- including the loopback bind, which must
    not start drifting to a LAN address just because a menu could now offer one.
    """

    work, env = _launcher_sandbox(tmp_path)
    env["GFMY_ONECLICK"] = "1"
    returncode, out = _run_launcher_on_tty(work, env, "\n")

    assert returncode == 0, out
    assert "How should the finished credentials reach" not in out, (
        "a preset GFMY_ONECLICK must suppress the handoff menu entirely."
    )
    argline = next(line for line in out.splitlines() if "DOCKER_ARGS: " in line)
    assert f"-f {ONECLICK_COMPOSE}" in argline
    bindline = next(line for line in out.splitlines() if "ONECLICK_BIND: " in line)
    assert "127.0.0.1" in bindline, (
        f"the preset path must keep the loopback default; got {bindline!r}"
    )


@pytest.mark.parametrize("track", ["a", "b", "c"])
def test_track_flag_suppresses_the_menu(tmp_path: Path, track: str) -> None:
    """``--track`` is the scriptable spelling of the menu and must skip it."""

    work, env = _launcher_sandbox(tmp_path)
    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, str(work / "login.sh"), "--track", track],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "How should the finished credentials reach" not in proc.stderr
    argline = next(
        line for line in proc.stdout.splitlines() if line.startswith("DOCKER_ARGS: ")
    )
    if track == "b":
        assert f"-f {ONECLICK_COMPOSE}" in argline
    else:
        assert ONECLICK_COMPOSE not in argline


def test_explicit_oneclick_bind_reaches_the_publish(tmp_path: Path) -> None:
    """An explicit ``GFMY_ONECLICK_BIND`` must win and reach Compose.

    This is the only route in a script or CI, where there is no menu to answer,
    so it has to work without a terminal.
    """

    work, env = _launcher_sandbox(tmp_path)
    env["GFMY_ONECLICK"] = "1"
    env["GFMY_ONECLICK_BIND"] = "192.0.2.10"
    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, str(work / "login.sh")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    bindline = next(
        line for line in proc.stdout.splitlines() if line.startswith("ONECLICK_BIND: ")
    )
    assert "192.0.2.10" in bindline, (
        f"GFMY_ONECLICK_BIND must reach the compose child; got {bindline!r}"
    )
    assert "UNENCRYPTED" in proc.stdout, (
        "a LAN bind of the token endpoint must warn about the clear-text transport"
    )


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", "*"])
def test_wildcard_oneclick_bind_is_refused(tmp_path: Path, wildcard: str) -> None:
    """A wildcard bind for 7901 must abort, not merely warn.

    For the noVNC viewer a wildcard only widens a password-gated viewer, and the
    launcher settles for a warning. This port hands out the credentials
    themselves, and a concrete address can always replace the wildcard, so there
    is no case in which continuing is the better answer.
    """

    work, env = _launcher_sandbox(tmp_path)
    env["GFMY_ONECLICK"] = "1"
    env["GFMY_ONECLICK_BIND"] = wildcard
    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, str(work / "login.sh")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )
    assert proc.returncode == 2, (
        f"a wildcard token-endpoint bind must exit 2; got {proc.returncode} "
        f"(stdout={proc.stdout!r})"
    )
    assert "wildcard" in proc.stderr, proc.stderr
    assert "DOCKER_ARGS: " not in proc.stdout, (
        "the launcher must refuse BEFORE starting the container"
    )


def test_login_cmd_mirrors_the_track_menu_and_the_bind_guard() -> None:
    """``login.cmd`` cannot be executed here (no Windows), so pin it by text.

    Every property the bash tests above prove behaviourally has a counterpart
    here, because a Windows user gets the same three tracks and the same refusal
    of a wildcard bind -- a launcher that silently offered less would be the
    quietest way for this hardening to go missing.
    """

    cmd = _read("login.cmd")
    assert "How should the finished credentials reach Home Assistant?" in cmd, (
        "login.cmd must offer the same handoff menu as login.sh."
    )
    assert 'set /p "TRACK_CHOICE=[login] Choice [Enter = A]: "' in cmd, (
        "the menu must default to track A on a bare Enter, like login.sh."
    )
    for guard in ("ONECLICK_ENV_SET", "CLEARTEXT_ENV_SET", "TRACK_FROM_CLI"):
        assert f"if defined {guard} goto :track_done" in cmd, (
            f"a preset {guard} must skip the menu, mirroring the bash precedence."
        )
    assert "GFMY_ONECLICK_BIND" in cmd.split("call :trim_trailing_blanks")[0], (
        "GFMY_ONECLICK_BIND must be in the trailing-blank trim list: `set VAR=1 && "
        "login.cmd` stores the blank, and an address with a trailing space becomes "
        "an invalid docker port bind."
    )
    assert re.search(r"^:reject_wildcard$", cmd, re.MULTILINE), (
        "login.cmd must define the wildcard refusal for the token endpoint."
    )
    wildcard_block = cmd.split("\n:oneclick_bind_wildcard\n", 1)[1].split("\n:", 1)[0]
    for token in ("popd", "endlocal", "exit /b 2"):
        assert token in wildcard_block, (
            f":oneclick_bind_wildcard must {token} like the other early exits."
        )
    assert "UNENCRYPTED" in cmd, (
        "login.cmd must warn about the clear-text transport on a LAN bind."
    )


def _dockerignore_rules() -> list[str]:
    """Return the effective (non-comment, non-blank) .dockerignore patterns, in order."""

    dockerignore = BUILD_CONTEXT / ".dockerignore"
    assert dockerignore.is_file(), (
        "A .dockerignore must exist at the build-context root "
        f"({BUILD_CONTEXT}) because the login image builds with `--build` and that "
        "context otherwise includes persisted OAuth/AAS credentials."
    )
    return [
        line.strip()
        for line in dockerignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_dockerignore_is_allowlist_excluding_every_secret_path() -> None:
    """.dockerignore must ignore the whole context and re-include only build inputs.

    A denylist that names only ``docker-login/data`` is whack-a-mole: the legacy
    ``Auth/secrets.json`` default of ``main.py::_resolve_secrets_path`` (and any
    future token/cache location) would still be uploaded with the ``--build``
    context. An allowlist -- ignore ``*``, then re-include only the paths the
    Dockerfile COPYs -- structurally keeps every secret out at once.
    """

    rules = _dockerignore_rules()

    # 1. The context is ignored wholesale by the very first effective rule.
    assert rules and rules[0] in {"*", "**"}, (
        ".dockerignore must start by ignoring the entire context (`*`), so that "
        "secrets in any location are excluded by default (allowlist model)."
    )

    # 2. The only `!`-re-includes may be the two Dockerfile COPY inputs plus the
    #    `docker-login` directory re-included SOLELY for traversal (Docker cannot
    #    descend into an ignored dir to reach entrypoint.sh). Never a secrets path.
    reincludes = {r[1:] for r in rules if r.startswith("!")}
    assert reincludes == {
        "requirements.txt",
        "docker-login",
        "docker-login/entrypoint.sh",
        "docker-login/start-novnc.sh",
    }, (
        "the only `!`-re-includes may be the Dockerfile's COPY inputs "
        "(requirements.txt, docker-login/entrypoint.sh, docker-login/start-novnc.sh) "
        f"plus `docker-login` for traversal; found {sorted(reincludes)}. Re-including "
        "anything else -- or the directory as a whole without re-excluding its "
        "contents -- risks shipping integration source or secrets into the image."
    )
    assert not any(
        r.startswith("!") and ("auth" in r.lower() or "secret" in r.lower())
        for r in rules
    ), ".dockerignore must never re-include an Auth/ or secrets path."

    # 3. The contents of the re-included docker-login/ are re-excluded WHOLESALE
    #    (`docker-login/*`) so a bare `!docker-login` cannot leak any other file
    #    placed under it (an exported secrets.json, a diagnostic log, a future
    #    addition) -- not merely docker-login/data. Order-sensitive (last match
    #    wins): `!docker-login` -> `docker-login/*` -> `!docker-login/entrypoint.sh`.
    assert "docker-login/*" in rules, (
        ".dockerignore must re-exclude the whole docker-login/ subtree "
        "(`docker-login/*`) after re-including the directory, so only the explicitly "
        "re-included entrypoint.sh survives -- not docker-login/data, an exported "
        "secrets.json, or any future file dropped under docker-login/."
    )
    i_dir = rules.index("!docker-login")
    i_star = rules.index("docker-login/*")
    i_entry = rules.index("!docker-login/entrypoint.sh")
    assert i_dir < i_star < i_entry, (
        "order must be `!docker-login` -> `docker-login/*` -> "
        "`!docker-login/entrypoint.sh` (last match wins), else the re-exclusion never "
        f"takes effect; got indices dir={i_dir}, star={i_star}, entry={i_entry}."
    )


def test_entrypoint_runs_handoff_from_exit_trap() -> None:
    """The ownership handoff must run via an EXIT trap, not only on the success path.

    Under ``set -e`` a nonzero ``main.py`` exit aborts the script; a linear handoff
    placed after ``main.py`` would be skipped, leaving the host unable to read or
    delete the container-owned credential file. Routing it through ``trap ... EXIT``
    guarantees it runs on failure and interruption too.
    """

    entrypoint = _read("entrypoint.sh")
    assert "trap cleanup EXIT" in entrypoint, (
        "entrypoint.sh must register the cleanup/handoff on EXIT so it also runs "
        "when main.py fails or is interrupted (set -e)."
    )
    # The handoff logic (host chown) must live inside the trapped cleanup function,
    # i.e. before it is registered on EXIT, not as trailing linear code.
    cleanup_def = entrypoint.find("function cleanup")
    trap_exit = entrypoint.find("trap cleanup EXIT")
    handoff = entrypoint.find("GFMY_HOST_UID", cleanup_def)
    assert 0 <= cleanup_def < handoff < trap_exit, (
        "the ownership handoff (GFMY_HOST_UID chown) must sit inside the cleanup() "
        "function that is trapped on EXIT."
    )


def test_entrypoint_forwards_signals_to_the_tracked_child() -> None:
    """SIGTERM/SIGINT must be forwarded to the CLI, which must run in the background.

    If ``python3 main.py`` runs in the FOREGROUND, bash defers the TERM/INT trap
    until the child exits on its own; a ``docker stop`` therefore never reaches the
    login process, Docker escalates to SIGKILL after its grace period, and the EXIT
    cleanup (ownership handoff + supervisor shutdown) is skipped -- leaving ``/data``
    owned by the container UID. The child must be backgrounded and waited on, and the
    signal traps must relay the signal to it, not merely ``exit``.
    """

    entrypoint = _read("entrypoint.sh")

    # 1) The CLI is started in the background and its PID captured. It is wrapped in a
    #    `( ... ) <&0 &` subshell that execs python (see the stdin/SIGINT guard below),
    #    so match the backgrounded exec form rather than a bare `python3 ... &`.
    assert "exec python3 main.py ${GFMY_ARGS:-} ) <&0 &" in entrypoint, (
        "main.py must be started in the BACKGROUND, otherwise bash defers the signal "
        "traps until it exits and docker stop hits SIGKILL."
    )
    assert "CHILD_PID=$!" in entrypoint, (
        "the entrypoint must capture the child PID (CHILD_PID=$!) so signals can "
        "be forwarded to it and its exit status waited on."
    )

    # 2) The entrypoint waits on that specific child (not a bare `wait`).
    assert 'wait "${CHILD_PID}"' in entrypoint, (
        "the entrypoint must wait on the tracked child PID so its real exit status "
        "propagates to the EXIT cleanup."
    )

    # 3) The TERM/INT traps forward to the child instead of exiting immediately.
    assert "trap 'on_signal TERM 143' TERM" in entrypoint
    assert "trap 'on_signal INT 130' INT" in entrypoint
    assert "trap 'exit 143' TERM" not in entrypoint, (
        "a bare `exit` on SIGTERM does not reach the foreground child; the trap must "
        "forward the signal to CHILD_PID (on_signal)."
    )
    assert "trap 'exit 130' INT" not in entrypoint

    # 4) on_signal actually relays the signal to the running child.
    assert 'kill -s "${sig}" "${CHILD_PID}"' in entrypoint, (
        "on_signal must relay the received signal to the tracked child via "
        "kill -s <sig> CHILD_PID."
    )


def test_entrypoint_preserves_stdin_and_sigint_for_background_child() -> None:
    """Backgrounding the CLI must not break the first-run login prompt or Ctrl-C.

    A non-interactive bash script with job control off applies two disruptive
    defaults to an async (``&``) child, both proven empirically:

    * its stdin is pointed at ``/dev/null`` unless an explicit redirection overrides
      it -- so ``main.py``'s ``input("Enter your Google account email:")`` prompt would
      hit EOF and abort the login (Codex: "Keep stdin attached when backgrounding the CLI");
    * SIGINT/SIGQUIT are hard-ignored (SIG_IGN), which Python inherits and then keeps
      instead of installing its ``KeyboardInterrupt`` handler -- so a relayed SIGINT
      is dropped and the re-wait loop hangs (Codex: "Restore SIGINT handling in the
      background child").

    The fix wraps the child in ``( trap - INT QUIT; exec python3 ... ) <&0 &``:
    ``<&0`` at the ASYNC BOUNDARY (not inside the subshell, where fd 0 is already
    ``/dev/null``) restores the terminal stdin, and ``trap - INT QUIT`` before ``exec``
    restores the default signal disposition so Python re-installs its handlers.
    """

    entrypoint = _read("entrypoint.sh")

    # stdin: the redirect must sit at the async boundary `) <&0 &`, not inside `( ... )`.
    assert "exec python3 main.py ${GFMY_ARGS:-} ) <&0 &" in entrypoint, (
        "the backgrounded CLI must restore terminal stdin via `<&0` at the async "
        "boundary, or the interactive `input()` login prompt reads EOF and aborts."
    )
    assert "<&0 ) &" not in entrypoint, (
        "the `<&0` redirect must be OUTSIDE the subshell (at the async boundary); "
        "inside the subshell fd 0 is already /dev/null, so it would be a no-op."
    )

    # SIGINT/SIGQUIT: reset to default inside the wrapper before exec.
    assert "trap - INT QUIT" in entrypoint, (
        "the wrapper must `trap - INT QUIT` before exec so bash's async SIG_IGN is "
        "cleared and Python installs its normal KeyboardInterrupt handler."
    )
    # The reset must precede the exec so the exec'd process inherits the default.
    reset_idx = entrypoint.index("trap - INT QUIT")
    exec_idx = entrypoint.index("exec python3 main.py")
    assert reset_idx < exec_idx, (
        "`trap - INT QUIT` must run BEFORE `exec python3` so the CLI child inherits "
        "the default SIGINT disposition."
    )


def _extract_backgrounding_line(entrypoint: str) -> str:
    """Return the real ``( ... exec python3 main.py ... ) <&0 &`` launch line.

    The behavioural test below runs the *actual* backgrounding construct taken
    verbatim from ``entrypoint.sh`` (only the executed program is swapped for a
    stub), so it fails if the file ever loses ``<&0`` or the ``trap - INT QUIT``
    reset -- it cannot pass on a regressed script the way a text guard could be
    fooled by matching a stale string elsewhere.
    """

    for line in entrypoint.splitlines():
        if "exec python3 main.py" in line and "<&0 &" in line:
            return line.strip()
    raise AssertionError(
        "could not locate the backgrounded `( ... exec python3 main.py ... ) <&0 &` "
        "launch line in entrypoint.sh"
    )


def test_backgrounded_login_child_behaviourally_keeps_stdin_and_default_sigint(
    tmp_path: Path,
) -> None:
    """Behavioural proof (not text matching) that the launch construct works.

    Codex asked to "cover the behavior rather than only matching script text".
    This test takes the REAL backgrounding line out of ``entrypoint.sh``, swaps
    only the executed program for a Python stub, runs it under a non-interactive
    ``bash`` script with job control off (the container condition) and asserts at
    runtime:

    * the child still reads the entrypoint's terminal stdin (``<&0`` works) -- the
      first-run account-e-mail prompt does not hit EOF; and
    * the child ends up with Python's DEFAULT ``SIGINT`` handler installed rather
      than the inherited ``SIG_IGN`` -- so a relayed Ctrl-C is not dropped.

    A negative control (same construct with ``<&0`` removed) must fail the stdin
    read, proving the assertion actually discriminates on the redirect rather
    than passing unconditionally.

    Reproduction limit (stated honestly): bash only forces ``SIG_IGN`` on an async
    child when it is attached to a controlling terminal, which a CI runner without
    a TTY does not provide, so this test verifies the POSITIVE end-state invariant
    (child has a working handler) which holds on every platform; the ordering that
    guarantees it under a TTY is locked by the text guard above.
    """

    import subprocess
    import sys

    entrypoint = _read("entrypoint.sh")
    launch = _extract_backgrounding_line(entrypoint)

    stub = tmp_path / "stub_child.py"
    stub.write_text(
        "import signal, sys\n"
        "try:\n"
        "    line = sys.stdin.readline().rstrip('\\n')\n"
        "except EOFError:\n"
        "    line = ''\n"
        "disp = signal.getsignal(signal.SIGINT)\n"
        "stdin_ok = line == 'ENTER-FROM-TERMINAL'\n"
        "sigint_ok = disp is not signal.SIG_IGN\n"
        "print('STDIN_OK=%s SIGINT_OK=%s' % (stdin_ok, sigint_ok))\n",
        encoding="utf-8",
    )

    # Rebuild the launch line with our stub in place of `main.py ${GFMY_ARGS:-}`,
    # keeping the surrounding `( trap - INT QUIT; exec python3 ... ) <&0 &` verbatim.
    stubbed = launch.replace("main.py ${GFMY_ARGS:-}", f"{stub.name!s}").replace(
        "python3", sys.executable
    )

    def _run(launch_line: str) -> str:
        script = (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            "trap 'on_signal INT 130' INT\n"
            "function on_signal { :; }\n"
            f"cd {tmp_path!s}\n"
            f"{launch_line}\n"
            "CHILD_PID=$!\n"
            'wait "${CHILD_PID}"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            input=b"ENTER-FROM-TERMINAL\n",
            capture_output=True,
            timeout=30,
            check=False,
        )
        return proc.stdout.decode()

    out = _run(stubbed)
    assert "STDIN_OK=True" in out, (
        f"backgrounded child did not receive terminal stdin via `<&0`; got: {out!r}"
    )
    assert "SIGINT_OK=True" in out, (
        f"backgrounded child kept the inherited SIG_IGN for SIGINT; got: {out!r}"
    )

    # Negative control: without the `<&0` async-boundary redirect the child's stdin
    # is /dev/null, so the read fails -- proving the assertion discriminates.
    control = stubbed.replace(") <&0 &", ") &")
    control_out = _run(control)
    assert "STDIN_OK=False" in control_out, (
        "control without `<&0` unexpectedly read stdin; the behavioural stdin "
        f"assertion is not discriminating. got: {control_out!r}"
    )


def _code_lines(script: str) -> list[str]:
    """Return only the non-comment, non-blank lines of a shell script.

    Every guard below that counts occurrences or matches a construct runs on this,
    so an explanatory comment can neither satisfy a guard (false negative: the
    construct is only *described*, not present) nor break one (false positive: a
    comment that merely mentions ``CHILD_PID=$!`` inflating a count).
    """

    return [
        ln
        for ln in script.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _extract_shell_function(entrypoint: str, name: str) -> str:
    """Return the verbatim ``function <name> { ... }`` block from ``entrypoint.sh``.

    The behavioural test below runs the REAL signal-handling helpers rather than a
    hand-written imitation, so it regresses with the script instead of drifting away
    from it. Relies on the file's own formatting: the body is indented and the block
    is closed by a ``}`` in column 0.
    """

    lines = entrypoint.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"function {name} {{"):
            for j in range(i + 1, len(lines)):
                if lines[j] == "}":
                    return "\n".join(lines[i : j + 1])
            break
    raise AssertionError(f"could not locate `function {name}` in entrypoint.sh")


def _extract_token_server_launch(entrypoint: str) -> str:
    """Return the real backgrounded ``token_server.py`` launch line.

    Feeding this verbatim into the behavioural test below is what makes it a
    WIRING test and not merely a construct test: if the server is ever moved back
    into the foreground, this raises instead of silently exercising a hand-written
    backgrounding line that no longer reflects the script.
    """

    for line in _code_lines(entrypoint):
        stripped = line.strip()
        if "exec python3" in stripped and "token_server.py" in stripped:
            if not stripped.endswith(") &"):
                break
            return stripped
    raise AssertionError(
        "could not locate a BACKGROUNDED `( ... exec python3 ... token_server.py ) &` "
        "launch line in entrypoint.sh (a foreground invocation defers the signal "
        "traps for up to the full TTL)"
    )


def test_every_tracked_wait_is_followed_by_the_shutdown_check() -> None:
    """Both waits must be guarded, not just the one the report happened to name.

    A tracked child dies on the relayed signal and its ``wait`` returns NORMALLY, so
    the status alone cannot distinguish "finished" from "shutting down" -- ``main.py``
    even catches ``KeyboardInterrupt`` and returns 0. Guarding only the token-server
    wait leaves the structurally identical CLI wait open: a relayed SIGINT would then
    look like a successful login and the run would continue into the handoff tracks,
    starting a token server nobody can reach or printing the bundle to the log.
    """

    code = _code_lines(_read("entrypoint.sh"))

    waits = [
        i
        for i, ln in enumerate(code)
        if ln.lstrip().startswith("wait_for_tracked_child")
    ]
    assert len(waits) == 2, f"expected exactly two tracked waits, found {len(waits)}"
    for i in waits:
        following = code[i + 1].lstrip()
        assert following.startswith("exit_if_terminating "), (
            "every wait_for_tracked_child must be followed immediately by "
            f"exit_if_terminating; the wait at {code[i].strip()!r} is followed by "
            f"{following!r}."
        )
    assert sum(ln.startswith("function exit_if_terminating {") for ln in code) == 1, (
        "the shutdown check must exist exactly once, as a shared helper."
    )


def test_wait_helper_clears_the_slot_and_ignores_an_empty_one() -> None:
    """The tracked slot must be reset, and an empty slot must not read as failure.

    Without the reset, ``on_signal`` keeps relaying to a PID number that the kernel
    may since have recycled to an unrelated process. And ``wait ""`` returns 1, which
    the ``-eq 0`` handoff gates would silently read as "the login failed".
    """

    helper = _extract_shell_function(_read("entrypoint.sh"), "wait_for_tracked_child")

    assert 'CHILD_PID=""' in helper, (
        "wait_for_tracked_child must clear the tracked slot before returning, so "
        "on_signal cannot signal a recycled PID in the gap between children."
    )
    assert '[ -n "${CHILD_PID}" ] || return 0' in helper, (
        "an empty slot must return success, not the exit status of a failed `wait`."
    )


def test_wait_helper_behaviourally_survives_a_child_that_outlives_one_signal(
    tmp_path: Path,
) -> None:
    """The re-wait loop -- the whole reason the helper exists -- must be exercised.

    ``wait`` returns >128 as soon as a forwarded signal interrupts it, even though the
    child is still shutting down. A child that handles the signal and exits with its
    own status a moment later is the case the loop is for; both other behavioural
    tests use a child that dies instantly and never enter it. Here the child traps
    TERM, sleeps, and exits 5 -- so a missing loop would surface as 143 instead.

    The stub is Python, matching the real children: a bash stub would have to hold
    its own ``sleep`` in the foreground and would therefore defer its own trap,
    reproducing the very defect under test instead of the shutdown being modelled.
    """

    entrypoint = _read("entrypoint.sh")
    on_signal = _extract_shell_function(entrypoint, "on_signal")
    wait_helper = _extract_shell_function(entrypoint, "wait_for_tracked_child")
    rc_file = tmp_path / "rc"

    stub = tmp_path / "slow_shutdown_child.py"
    stub.write_text(
        "import signal, sys, time\n"
        "def _handler(signum, frame):\n"
        "    time.sleep(1)  # graceful shutdown still in progress\n"
        "    sys.exit(5)\n"
        "signal.signal(signal.SIGTERM, _handler)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    script = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'CHILD_PID=""\n'
        '_terminating=""\n'
        f"{on_signal}\n"
        f"{wait_helper}\n"
        "trap 'on_signal TERM 143' TERM\n"
        f"( trap - INT QUIT; exec {sys.executable} {stub!s} ) &\n"
        "CHILD_PID=$!\n"
        "_rc=0\n"
        "wait_for_tracked_child || _rc=$?\n"
        f'echo "${{_rc}}" > "{rc_file!s}"\n'
    )
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        ["bash", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        time.sleep(0.5)
        proc.terminate()
        proc.wait(timeout=15)
    finally:
        proc.kill()
        proc.wait(timeout=15)

    assert rc_file.exists(), "the helper never returned after the forwarded signal"
    assert rc_file.read_text().strip() == "5", (
        "the helper returned the interrupted-wait status instead of re-waiting for "
        f"the child's real exit code; got {rc_file.read_text().strip()!r}."
    )


def test_terminating_marker_catches_a_child_that_exits_cleanly_on_the_signal(
    tmp_path: Path,
) -> None:
    """The ``_terminating`` half of the shutdown check must not be dead weight.

    The status check (``_srv_rc > 128``) alone covers today's server, which returns
    130 on ``KeyboardInterrupt``. It does NOT cover a child that catches the signal
    and shuts down cleanly with 0 -- an entirely ordinary way to write a server, and
    one change away. Then only the marker set in ``on_signal`` still says "we are
    shutting down", and without it execution would fall through into the clear-text
    track on a plain ``docker stop``. Both halves are kept for that reason; this test
    pins the half the status check cannot reach.
    """

    entrypoint = _read("entrypoint.sh")
    on_signal = _extract_shell_function(entrypoint, "on_signal")
    wait_helper = _extract_shell_function(entrypoint, "wait_for_tracked_child")
    guard = (
        f"{_extract_shell_function(entrypoint, 'exit_if_terminating')}\n"
        'exit_if_terminating "${_srv_rc}"'
    )
    fallthrough = tmp_path / "fallthrough"

    stub = tmp_path / "clean_exit_child.py"
    stub.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    script = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'CHILD_PID=""\n'
        '_terminating=""\n'
        f"{on_signal}\n"
        f"{wait_helper}\n"
        "trap 'on_signal TERM 143' TERM\n"
        f"( trap - INT QUIT; exec {sys.executable} {stub!s} ) &\n"
        "CHILD_PID=$!\n"
        "_srv_rc=0\n"
        "wait_for_tracked_child || _srv_rc=$?\n"
        f"{guard}\n"
        f'echo reached > "{fallthrough!s}"\n'
    )
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        ["bash", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        time.sleep(0.5)
        proc.terminate()
        proc.wait(timeout=15)
    finally:
        proc.kill()
        proc.wait(timeout=15)

    assert not fallthrough.exists(), (
        "a child that exited 0 on the relayed signal was treated as a normal "
        "completion, so execution continued into the post-server handoff tracks."
    )


def test_entrypoint_tracks_the_oneclick_token_server_like_the_cli_child() -> None:
    """The One-Click token server must NOT run as a foreground child.

    Codex: with ``GFMY_ONECLICK=1`` the server can wait for its full TTL (300 s).
    Bash is PID 1 here (exec-form ``ENTRYPOINT``) and defers its TERM/INT traps until
    the foreground child returns, so a ``docker stop`` during that wait escalates to
    SIGKILL after the default 10 s grace period: the EXIT cleanup never runs, ``/data``
    keeps container ownership and the 0600 bundle stays unreadable for the host user.
    This is the exact failure the CLI launch already avoids -- the server has to use
    the same tracked-child construct.
    """

    entrypoint = _read("entrypoint.sh")
    code = _code_lines(entrypoint)

    launch = _extract_token_server_launch(entrypoint)  # raises on a foreground call
    assert launch.endswith(") &"), (
        "the token server must be started in the BACKGROUND (`( ... ) &`), otherwise "
        "bash defers the signal traps for up to the full TTL."
    )
    assert not any(
        ln.strip() == "python3 /app/gfmy/docker-login/token_server.py || true"
        for ln in code
    ), "the bare foreground `python3 token_server.py || true` invocation must be gone."
    assert "trap - INT QUIT; exec python3" in launch, (
        "the server child must reset INT/QUIT before exec so Python installs its own "
        "KeyboardInterrupt handler instead of inheriting SIG_IGN."
    )
    # Both long-running children go through the ONE tracked slot and the ONE wait
    # helper; a second hand-rolled re-wait loop would be a copy waiting to drift.
    assert sum(ln.strip() == "CHILD_PID=$!" for ln in code) == 2, (
        "both the CLI child and the token server must publish their PID into the "
        "single tracked slot on_signal relays to."
    )
    definitions = [
        ln for ln in code if ln.startswith("function wait_for_tracked_child {")
    ]
    call_sites = [ln for ln in code if ln.lstrip().startswith("wait_for_tracked_child")]
    assert len(definitions) == 1, (
        "the signal-interrupted re-wait loop must exist exactly once, as a shared "
        f"helper; found {len(definitions)} definitions."
    )
    assert len(call_sites) == 2, (
        "expected exactly two call sites of the shared wait helper (CLI child and "
        f"token server), no per-call-site copy of the loop; found {len(call_sites)}."
    )


def test_tracked_child_behaviourally_lets_cleanup_run_on_sigterm(
    tmp_path: Path,
) -> None:
    """Behavioural proof that a signal reaches cleanup while a long child is waiting.

    Runs the REAL ``on_signal`` / ``wait_for_tracked_child`` helpers AND the real
    token-server launch line, all taken verbatim out of ``entrypoint.sh``, around a
    long-sleeping stub child; sends SIGTERM to the outer bash and asserts the
    EXIT-trap cleanup runs PROMPTLY -- i.e. well within a Docker stop grace period
    rather than only after the child's own timeout. Taking the launch line from the
    file (rather than writing one here) is what also makes this a wiring test: moving
    the server back into the foreground fails extraction instead of quietly passing.

    The negative control is the construct Codex flagged: the very same script with the
    child in the FOREGROUND. There bash defers the trap until the child returns, so no
    cleanup marker appears in time -- which is what makes the positive assertion
    discriminating rather than vacuously true.
    """

    entrypoint = _read("entrypoint.sh")
    on_signal = _extract_shell_function(entrypoint, "on_signal")
    wait_helper = _extract_shell_function(entrypoint, "wait_for_tracked_child")

    # The child outlives the observation window by far, standing in for a token
    # server that is still waiting for an ack (up to its 300 s TTL). Only the
    # executed program is swapped; the surrounding construct stays verbatim.
    child_sleep = 30
    grace = 5.0
    real_launch = _extract_token_server_launch(entrypoint)
    stub_launch = re.sub(
        r"exec python3 \S*token_server\.py", f"exec sleep {child_sleep}", real_launch
    )
    assert "exec sleep" in stub_launch, (
        f"could not substitute the stub into the real launch line: {real_launch!r}"
    )

    guard = (
        f"{_extract_shell_function(entrypoint, 'exit_if_terminating')}\n"
        'exit_if_terminating "${_srv_rc}"'
    )

    def _run(tracked: bool, *, with_guard: bool = True) -> bool:
        tag = f"{'tracked' if tracked else 'foreground'}_{int(with_guard)}"
        marker = tmp_path / f"cleanup_{tag}"
        # Stands in for the clear-text track that follows the server wait: it must
        # NOT be reached when the run is ending because of a signal.
        fallthrough = tmp_path / f"fallthrough_{tag}"
        launch = (
            f"{stub_launch}\n"
            "CHILD_PID=$!\n"
            "_srv_rc=0\n"
            "wait_for_tracked_child || _srv_rc=$?\n"
            f"{guard if with_guard else ''}\n"
            if tracked
            else f"sleep {child_sleep} || true\n"
        )
        script = (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            "_cleaned=0\n"
            'CHILD_PID=""\n'
            '_terminating=""\n'
            "function cleanup {\n"
            '  [ "${_cleaned}" = 1 ] && return\n'
            "  _cleaned=1\n"
            f'  echo ran > "{marker!s}"\n'
            "}\n"
            f"{on_signal}\n"
            f"{wait_helper}\n"
            "trap cleanup EXIT\n"
            "trap 'on_signal TERM 143' TERM\n"
            f"{launch}"
            f'echo reached > "{fallthrough!s}"\n'
        )
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            ["bash", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.5)  # let the child start and the wait settle
            proc.terminate()
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
            return marker.exists(), fallthrough.exists()
        finally:
            proc.kill()
            proc.wait(timeout=grace)

    cleaned, fell_through = _run(tracked=True)
    assert cleaned, (
        "the EXIT cleanup did not run within the grace window while a long-lived "
        "tracked child was being waited on; a `docker stop` would escalate to "
        "SIGKILL and skip the /data ownership handoff."
    )
    # Backgrounding alone is not enough: the tracked child dies on the relayed
    # signal and its wait() returns NORMALLY, so without the terminating check the
    # script would continue into the clear-text track and dump the credential
    # bundle into the container log on a plain `docker stop`.
    assert not fell_through, (
        "execution continued into the post-server handoff tracks after a relayed "
        "SIGTERM; a `docker stop` would print the credential bundle to the "
        "container log and report success."
    )

    control_cleaned, _ = _run(tracked=False)
    assert not control_cleaned, (
        "control with a FOREGROUND child unexpectedly ran cleanup in time, so this "
        "test does not actually discriminate between the two constructs."
    )

    # Second control: the same tracked construct WITHOUT the terminating check does
    # fall through -- proving the assertion above discriminates on that guard and is
    # not merely satisfied by the backgrounding.
    _, unguarded_fell_through = _run(tracked=True, with_guard=False)
    assert unguarded_fell_through, (
        "control without the terminating check unexpectedly stopped anyway, so the "
        "fall-through assertion does not discriminate on that guard."
    )


def _docker_context_excluded(rules: list[str], path: str) -> bool:
    """Return True if ``path`` is EXCLUDED from the Docker build context by ``rules``.

    Faithfully models moby/patternmatcher (Docker's ``.dockerignore`` engine): the
    patterns are evaluated in order and the LAST pattern that matches the path -- or
    any of its parent directories -- decides; a plain pattern excludes, a
    ``!``-prefixed pattern re-includes. ``*`` matches a single path segment (it does
    not cross ``/``), per Go ``filepath.Match``. This "last-match-wins + parent-dir
    descent" behaviour is exactly why a bare ``!docker-login`` re-includes the whole
    subtree and why ``docker-login/*`` + ``!docker-login/entrypoint.sh`` is needed to
    let through only the one file. (Verified to agree with the ``pathspec`` reference
    matcher on the representative paths below; re-implemented inline so the guard adds
    no test-only dependency to the Poetry env.)
    """

    import fnmatch
    from pathlib import PurePosixPath

    posix = PurePosixPath(path)
    candidates = [str(posix)] + [str(par) for par in posix.parents if str(par) != "."]

    def _matches(pattern: str, candidate: str) -> bool:
        pat_parts = pattern.rstrip("/").split("/")
        cand_parts = candidate.split("/")
        if len(pat_parts) != len(cand_parts):
            return False
        return all(
            fnmatch.fnmatchcase(seg, pat)
            for pat, seg in zip(pat_parts, cand_parts, strict=True)
        )

    excluded = False
    for rule in rules:
        negated = rule.startswith("!")
        pattern = rule[1:] if negated else rule
        if any(_matches(pattern, cand) for cand in candidates):
            excluded = not negated
    return excluded


def test_dockerignore_behaviourally_excludes_files_dropped_under_docker_login() -> None:
    """Behavioural proof that the allowlist keeps stray secrets out of the context.

    Codex flagged that a bare ``!docker-login`` re-includes the ENTIRE directory, so a
    credential export / diagnostic log / any file placed under ``docker-login/`` other
    than ``data/`` (the only path the old denylist named) would be uploaded to the
    Docker daemon, contradicting the stated allowlist guarantee. Rather than only
    matching pattern text, this test EVALUATES the real ``.dockerignore`` with a
    faithful model of Docker's matcher and asserts the include/exclude DECISION for
    representative paths, with a discriminating control on the old rules that proves
    the leak the fix closes.
    """

    rules = _dockerignore_rules()

    # The two Dockerfile COPY inputs must stay IN the context, or the build breaks.
    assert not _docker_context_excluded(rules, "requirements.txt")
    assert not _docker_context_excluded(rules, "docker-login/entrypoint.sh")

    # Every secret/stray location is OUT -- including a file dropped directly under
    # docker-login/ (the exact hole Codex reported), not just docker-login/data.
    for secret in (
        "docker-login/secrets.json",  # exported straight into docker-login/
        "docker-login/diagnostic.log",  # a stray diagnostic artifact
        "docker-login/data/secrets.json",  # the container's writable volume
        "Auth/secrets.json",  # legacy _resolve_secrets_path default
        "token_cache.json",  # any future top-level token/cache file
    ):
        assert _docker_context_excluded(rules, secret), (
            f"{secret!r} must be excluded from the build context by the allowlist"
        )

    # Discriminating control: the previous rules (bare `!docker-login`, only
    # `docker-login/data` re-excluded) LEAK a file dropped directly under
    # docker-login/. This proves the matcher -- and thus the assertions above --
    # actually detect the regression the fix removes (mutation gegenprobe baked in).
    old_rules = ["*", "!requirements.txt", "!docker-login", "docker-login/data"]
    assert not _docker_context_excluded(old_rules, "docker-login/secrets.json"), (
        "sanity: the old denylist rules must (wrongly) keep docker-login/secrets.json "
        "in context, otherwise this test would pass even without the fix"
    )
    assert _docker_context_excluded(rules, "docker-login/secrets.json"), (
        "the fixed allowlist must exclude docker-login/secrets.json"
    )


def test_readme_container_station_does_not_present_compose_up_first_login() -> None:
    """Container Station guidance must not present compose-up as a first-login path.

    Codex: importing docker-compose.yml as a Container Station *application* starts it
    with ``docker compose up`` semantics, which does not forward terminal STDIN, so the
    first-run account-e-mail prompt can never be answered. Telling users to "keep
    it in the foreground for the first (login) run" is therefore wrong (foreground is
    not stdin attached). The README must route the interactive login through the
    stdin-attaching ``docker compose run`` (SSH) path and only offer Container Station
    for later, already-authenticated runs.
    """

    readme = _read("README.md")

    assert "**Container Station:**" in readme, (
        "README must document the Container Station option so this guard stays anchored."
    )
    # Isolate the Container Station bullet (up to the next blank line).
    bullet = readme.split("**Container Station:**", 1)[1].split("\n\n", 1)[0].lower()

    # It must NOT claim the first/login run works by keeping the compose-up
    # application "in the foreground" (the exact wrong claim Codex flagged).
    assert not ("foreground" in bullet and "first" in bullet and "login" in bullet), (
        "Container Station bullet still presents keeping the compose-up application "
        "'in the foreground' for the 'first (login)' run -- that path has no stdin, so "
        "the interactive login cannot proceed."
    )
    # It must steer the login to the stdin-attaching `docker compose run` path...
    assert "compose run" in bullet, (
        "Container Station bullet must route the interactive login to the "
        "`docker compose run` path (the only one that attaches stdin)."
    )
    # ...and warn that the app-import / compose-up path does not forward a terminal.
    assert "compose up" in bullet and "forward" in bullet, (
        "Container Station bullet must warn that the app-import/compose-up path does "
        "not forward a terminal, so the first-login prompt stalls."
    )


# The post-login track dispatch of entrypoint.sh: from the resolved secrets path
# down to (but excluding) the final `exit "${_rc}"`. The behavioural test below
# runs this section VERBATIM (only the token-server program is swapped for a
# stub), so it exercises the real branch structure instead of a paraphrase and
# cannot be satisfied by a comment that merely claims the tracks are independent.
_HANDOFF_START = '_secrets_path="${GOOGLEFINDMY_SECRETS_PATH:-/data/secrets.json}"'
_HANDOFF_END = 'exit "${_rc}"'
_TOKEN_SERVER_PATH = "/app/gfmy/docker-login/token_server.py"


def _extract_handoff_section(entrypoint: str) -> str:
    """Return the post-login track dispatch of ``entrypoint.sh``."""

    start = entrypoint.find(_HANDOFF_START)
    assert start != -1, (
        "could not locate the `_secrets_path=` anchor that starts the post-login "
        "handoff section in entrypoint.sh"
    )
    end = entrypoint.find(_HANDOFF_END, start)
    assert end != -1, (
        'could not locate the trailing `exit "${_rc}"` that ends the post-login '
        "handoff section in entrypoint.sh"
    )
    return entrypoint[start:end]


def _as_elif_chain(section: str) -> str:
    """Re-create the pre-fix if/elif chain (mutation control for the guard below)."""

    chained = section.replace(
        'if [ "${_rc}" -eq 0 ] && [ "${GFMY_CLEARTEXT:-}" = "1" ]',
        'elif [ "${_rc}" -eq 0 ] && [ "${GFMY_CLEARTEXT:-}" = "1" ]',
        1,
    )
    assert chained != section, "clear-text condition not found; control is stale"
    # Drop the `fi` that closes the one-click block, so the clear-text branch really
    # chains onto it. Located structurally (last top-level `fi` before the `elif`)
    # rather than by a text anchor next to the server call, which moves whenever the
    # one-click block gains a line.
    lines = chained.splitlines()
    elif_at = next(i for i, ln in enumerate(lines) if ln.startswith("elif "))
    close_at = max(i for i in range(elif_at) if lines[i] == "fi")
    chained = "\n".join(lines[:close_at] + lines[close_at + 1 :]) + "\n"
    assert chained.count("\nfi\n") == 1, (
        "control must leave exactly the chain-closing `fi`; got "
        f"{chained.count(chr(10) + 'fi' + chr(10))}"
    )
    return chained


def _run_handoff_section(
    section: str,
    *,
    tmp_path: Path,
    oneclick: bool,
    cleartext: bool,
    server_consumes_secret: bool,
) -> tuple[str, bool]:
    """Execute the track dispatch with a stubbed token server.

    ``server_consumes_secret`` models the two token-server outcomes that matter
    here: ``True`` = ack/TTL (the server deleted ``secrets.json``), ``False`` =
    lockout (the server deliberately KEPT the file). Returns the captured stdout
    and whether the secrets file still exists afterwards.
    """

    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text('{"token": "SECRET-BUNDLE-MARKER"}\n', encoding="utf-8")

    server_stub = tmp_path / "token_server_stub.py"
    server_stub.write_text(
        "import os\n"
        "if os.environ['STUB_CONSUMES_SECRET'] == '1':\n"
        "    os.remove(os.environ['GOOGLEFINDMY_SECRETS_PATH'])\n"
        "print('[stub] token server returned')\n",
        encoding="utf-8",
    )

    script = section.replace(_TOKEN_SERVER_PATH, str(server_stub)).replace(
        "python3", sys.executable
    )
    # The extracted section calls the tracked-child helpers, which are defined
    # ABOVE it in entrypoint.sh. Without them bash reports "command not found",
    # never waits for the backgrounded server, and the dispatch below races the
    # stub -- a green-looking harness measuring nothing. Prepend the REAL helpers
    # (and the state they own) so the section runs in its actual context.
    entrypoint = _read("entrypoint.sh")
    preamble = (
        "set -e\n"
        "_rc=0\n"
        'CHILD_PID=""\n'
        '_terminating=""\n'
        f"{_extract_shell_function(entrypoint, 'on_signal')}\n"
        f"{_extract_shell_function(entrypoint, 'wait_for_tracked_child')}\n"
        f"{_extract_shell_function(entrypoint, 'exit_if_terminating')}\n"
    )
    proc = subprocess.run(
        ["bash", "-c", preamble + script],
        env={
            **os.environ,
            "GFMY_ONECLICK": "1" if oneclick else "",
            "GFMY_CLEARTEXT": "1" if cleartext else "",
            "GOOGLEFINDMY_SECRETS_PATH": str(secrets_file),
            "STUB_CONSUMES_SECRET": "1" if server_consumes_secret else "0",
        },
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, (
        f"handoff section exited {proc.returncode}: {proc.stderr.decode()!r}"
    )
    return proc.stdout.decode(), secrets_file.exists()


def test_cleartext_track_runs_after_the_oneclick_server_even_with_both_switches(
    tmp_path: Path,
) -> None:
    """``GFMY_ONECLICK=1`` must not swallow ``GFMY_CLEARTEXT=1`` (Codex P2, PR #1211).

    The two switches are documented as independent in ``docker-compose.yml`` and
    ``README.md``. While the clear-text track hung off the one-click branch as an
    ``elif``, setting both selected one-click and the ``elif`` was never evaluated:
    in the documented LOCKOUT case the token server keeps ``secrets.json`` on
    purpose precisely so the fallbacks still work, yet Track C printed nothing and
    the user lost the fallback they asked for.

    This is a behavioural guard: it takes the REAL dispatch out of
    ``entrypoint.sh``, stubs only the token server, and asserts the printed output
    for the three outcomes. The mutation control re-creates the ``elif`` chain and
    must fail to print the block -- so the test discriminates on the structure, not
    on the comment next to it.
    """

    section = _extract_handoff_section(_read("entrypoint.sh"))

    # 1) Both switches, LOCKOUT: the server kept the file, so Track C must print it
    #    and then remove it (the file is not left lying around).
    out, still_there = _run_handoff_section(
        section,
        tmp_path=tmp_path,
        oneclick=True,
        cleartext=True,
        server_consumes_secret=False,
    )
    assert "ONE-CLICK login ready" in out, (
        f"one-click track did not run with GFMY_ONECLICK=1; got: {out!r}"
    )
    assert "BEGIN secrets.json (copy)" in out and "SECRET-BUNDLE-MARKER" in out, (
        "with GFMY_ONECLICK=1 AND GFMY_CLEARTEXT=1 the clear-text fallback must "
        "still print the bundle the server kept after a lockout; got: " + repr(out)
    )
    assert not still_there, (
        "Track C must remove the printed secrets.json so nothing lingers on disk"
    )

    # 2) Both switches, ACK/TTL: the server consumed (deleted) the bundle, so there
    #    is nothing left to print -- no empty/misleading block.
    out, still_there = _run_handoff_section(
        section,
        tmp_path=tmp_path,
        oneclick=True,
        cleartext=True,
        server_consumes_secret=True,
    )
    assert "ONE-CLICK login ready" in out
    assert "BEGIN secrets.json (copy)" not in out, (
        "after the server consumed (deleted) secrets.json the clear-text block must "
        f"NOT be printed; got: {out!r}"
    )
    assert not still_there

    # 3) Clear-text alone: the historical path is unchanged (print, then remove).
    out, still_there = _run_handoff_section(
        section,
        tmp_path=tmp_path,
        oneclick=False,
        cleartext=True,
        server_consumes_secret=False,
    )
    assert "ONE-CLICK login ready" not in out
    assert "SECRET-BUNDLE-MARKER" in out
    assert not still_there

    # 4) Mutation control: with the pre-fix ``elif`` chain the same lockout scenario
    #    prints the one-click banner but NOT the clear-text block -- proving the
    #    assertion in (1) fails on a regressed script instead of passing anyway.
    control_out, control_still_there = _run_handoff_section(
        _as_elif_chain(section),
        tmp_path=tmp_path,
        oneclick=True,
        cleartext=True,
        server_consumes_secret=False,
    )
    assert "ONE-CLICK login ready" in control_out, (
        "control did not even reach the one-click branch; it proves nothing. "
        f"got: {control_out!r}"
    )
    assert "BEGIN secrets.json (copy)" not in control_out, (
        "sanity: the pre-fix elif chain must SKIP the clear-text track, otherwise "
        f"this guard would pass without the fix. got: {control_out!r}"
    )
    assert control_still_there, (
        "sanity: with the elif chain nothing prints or removes the kept bundle"
    )


def _entrypoint_code_lines() -> list[str]:
    """Return ``entrypoint.sh`` without comment-only and blank lines.

    Structural assertions run on this projection so a guard can never be satisfied
    by prose in a comment that describes the intended shape.
    """

    return _code_lines(_read("entrypoint.sh"))


def test_entrypoint_keeps_one_clear_text_block_in_its_own_if(tmp_path: Path) -> None:
    """The clear-text track is one block, in its own ``if``, after the one-click one.

    Structural companion to the behavioural guard above: it pins the two properties
    that behaviour alone cannot show -- that the fix was not implemented by
    DUPLICATING the print block into both branches (the block must exist exactly
    once and be reachable from both paths), and that the clear-text condition is a
    top-level ``if`` placed after the one-click block was closed with ``fi``.
    """

    del tmp_path  # structural guard: no filesystem scenario needed

    code = _entrypoint_code_lines()

    cleartext_conditions = [
        i for i, line in enumerate(code) if "GFMY_CLEARTEXT" in line
    ]
    assert len(cleartext_conditions) == 1, (
        "expected exactly one GFMY_CLEARTEXT condition in entrypoint.sh; got "
        f"{[code[i] for i in cleartext_conditions]}"
    )
    cleartext_idx = cleartext_conditions[0]
    assert code[cleartext_idx].startswith("if "), (
        "the clear-text track must be its OWN top-level `if`, not an `elif` chained "
        f"to the one-click branch; got: {code[cleartext_idx]!r}"
    )

    # It comes AFTER the one-click server call, and that branch is closed first.
    server_idx = next(
        i
        for i, line in enumerate(code)
        if _TOKEN_SERVER_PATH.rsplit("/", 1)[-1] in line
    )
    assert server_idx < cleartext_idx, (
        "the clear-text track must be evaluated AFTER the token server returned"
    )
    assert any(line.strip() == "fi" for line in code[server_idx:cleartext_idx]), (
        "the one-click branch must be closed with `fi` before the clear-text `if`"
    )

    # DRY: exactly one clear-text print block, and it re-tests the file's existence.
    assert sum("BEGIN secrets.json" in line for line in code) == 1, (
        "the clear-text block must exist exactly ONCE and be reachable from both "
        "paths; duplicating it into the one-click branch is not the fix."
    )
    assert '[ -f "${_secrets_path}" ]' in code[cleartext_idx], (
        "the clear-text condition must re-test the secrets file, so an acked or "
        "TTL-consumed (deleted) bundle prints nothing."
    )


def _echo_lines(launcher: str) -> list[str]:
    """Return the launcher lines that actually PRINT something to the user.

    Only these lines are the user-facing contract; the surrounding comment
    blocks may (and do) still discuss ``localhost`` as a concept. For bash an
    output line starts with ``echo`` after optional indentation; for the batch
    file the ``echo`` may sit behind a one-line ``if`` guard, so any line
    carrying an ``echo`` token counts, minus the ``rem`` comments.
    """

    lines = _read(launcher).splitlines()
    if launcher.endswith(".cmd"):
        return [
            line
            for line in lines
            if not line.lstrip().lower().startswith("rem")
            and re.search(r"\becho\b", line, re.IGNORECASE)
        ]
    return [line for line in lines if re.match(r"\s*echo\b", line)]


@pytest.mark.parametrize("launcher", ["login.sh", "login.cmd"])
def test_launcher_never_prints_localhost_as_the_novnc_url(launcher: str) -> None:
    """No printed line may hand the user ``localhost:7900`` as the noVNC address.

    The noVNC viewer is opened by the operator's BROWSER, which in the common
    setup does not run on the Docker host at all (NAS, home server, VM). A
    printed ``http://localhost:7900`` then points the browser at the wrong
    machine and the login appears broken. The launchers therefore print the
    address they actually bound (or the one passed via ``--ip``), never the
    ``localhost`` alias.
    """

    offenders = [line for line in _echo_lines(launcher) if "localhost:7900" in line]
    assert not offenders, (
        f"{launcher} still prints a localhost noVNC URL ({offenders!r}). Print the "
        "bound/--ip address instead: the browser that opens this URL usually runs "
        "on a different machine than the Docker host."
    )


def test_login_sh_handles_the_ip_flag_in_its_argument_parser() -> None:
    """``login.sh`` must handle ``--ip`` in its argument parser.

    ``--ip <ADDRESS>`` is the supported way to make the noVNC viewer reachable
    from another machine, and it has to be handled by the argument parser
    itself, not merely mentioned in the usage text (which is the failure mode a
    plain substring check would miss).

    The token endpoint (7901) has its own, deliberately narrower opt-in:
    ``GFMY_ONECLICK_BIND``, defaulting to loopback, guarded by
    ``test_token_port_has_exactly_one_bind_knob_defaulting_to_loopback`` above and
    by the launcher checks (wildcard refused, clear-text warning) further down. It
    has no ``--ip``-style flag, so this test is about ``--ip`` only.
    """

    text = _read("login.sh")
    # `--ip` must be handled inside the argument-dispatch LOOP. Anchoring on the
    # first `case "$1" in` would be wrong: the address-validation helpers use
    # that construct too, so the guard would pass on their text alone.
    lines = text.splitlines()
    loop_start = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip() == 'while [ "$#" -gt 0 ]; do'
        ),
        None,
    )
    assert loop_start is not None, (
        'login.sh must dispatch its arguments through a `while [ "$#" -gt 0 ]` '
        "loop; the --ip guard below anchors on it."
    )
    loop_end = next(
        i
        for i, line in enumerate(lines[loop_start:], loop_start)
        if line.strip() == "done"
    )
    parser = "\n".join(lines[loop_start:loop_end])
    assert "--ip" in parser, (
        "login.sh must handle `--ip` in its argument parser (documenting it in the "
        "usage text alone leaves the flag unimplemented)."
    )
    assert "--ip=*" in parser, (
        "login.sh must accept the `--ip=<addr>` spelling too; the README promises "
        "both spellings for parity with login.cmd."
    )


def test_reachability_hint_is_derived_from_the_bind_not_the_printed_address() -> None:
    """The "who can reach this" verdict must key on the BIND in both launchers.

    With a wildcard bind the PRINTED address is a concrete one (loopback for
    ``login.cmd``, a detected address for ``login.sh``). Branching the hint on
    that printed value therefore claims "only this Docker host reaches it" for a
    viewer that is in fact listening on every interface, protected by the fixed
    password ``secret`` -- a false all-clear in exactly the security note whose
    job is to be true. This regression shipped once and is pinned here.
    """

    sh = _read("login.sh")
    assert 'if is_loopback_addr "$novnc_bind"; then' in sh, (
        "login.sh must classify the BIND for the reachability hint; branching on "
        "$novnc_url_host gives a false all-clear for a wildcard bind."
    )
    assert 'case "$novnc_url_host" in' not in sh, (
        "login.sh must not branch its reachability hint on the printed address."
    )

    cmd = _read("login.cmd")
    assert "if defined IS_LOOPBACK goto :loopback_hint" in cmd, (
        "login.cmd must branch its reachability hint on the IS_LOOPBACK flag."
    )
    assert '"%NOVNC_URL_HOST%"=="127.0.0.1" goto :loopback_hint' not in cmd, (
        "login.cmd must not branch its reachability hint on the printed address."
    )
    loopback_flags = [
        line for line in cmd.splitlines() if 'set "IS_LOOPBACK=1"' in line
    ]
    assert loopback_flags, "login.cmd must set IS_LOOPBACK somewhere."
    assert all("%NOVNC_BIND" in line for line in loopback_flags), (
        "every IS_LOOPBACK assignment must be derived from %NOVNC_BIND%, not "
        f"from the printed address (found {loopback_flags!r})."
    )


def test_login_cmd_argument_parser_handles_both_ip_spellings() -> None:
    """``login.cmd`` must implement ``--ip X`` and ``--ip=X``, with a miss path.

    The README promises "the same ``--ip`` flag" as ``login.sh``. Batch has no
    argument parser, so the flag lives in hand-written labels; a missing label
    or a lost ``shift`` silently turns the flag into "unknown option". Every
    label referenced by a ``goto`` must therefore also be defined.
    """

    cmd = _read("login.cmd")
    for needle in (
        'if /i "%~1"=="--ip" goto :parse_ip',
        'if /i "%ARG:~0,5%"=="--ip=" goto :parse_ip_inline',
        ":parse_ip_inline",
        ":ip_missing",
    ):
        assert needle in cmd, f"login.cmd must contain {needle!r}"

    targets = set(re.findall(r"goto :([A-Za-z_][A-Za-z0-9_]*)", cmd))
    targets |= set(re.findall(r"call :([A-Za-z_][A-Za-z0-9_]*)", cmd))
    defined = set(re.findall(r"^:([A-Za-z_][A-Za-z0-9_]*)", cmd, re.MULTILINE))
    defined.add("eof")  # `goto :eof` is a cmd.exe builtin, never a real label
    missing = sorted(targets - defined)
    assert not missing, f"login.cmd jumps to undefined labels: {missing}"


def test_launchers_bracket_ipv6_before_printing_or_binding() -> None:
    """Both launchers must normalise an IPv6 literal to exactly one bracket pair.

    The ``--ip`` validators accept the bare and the bracketed spelling, so a bare
    ``2001:db8::1`` copied straight into the URL yields
    ``http://2001:db8::1:7900``, which no browser can parse, and docker's port
    syntax needs the brackets as well.
    """

    sh = _read("login.sh")
    assert "bracket_if_ipv6()" in sh, "login.sh must define bracket_if_ipv6()"
    for target in (
        'novnc_url_host="$(bracket_if_ipv6 "$novnc_url_host")"',
        'novnc_bind="$(bracket_if_ipv6 "$novnc_bind")"',
    ):
        assert target in sh, f"login.sh must normalise through {target!r}"

    cmd = _read("login.cmd")
    assert re.search(r"^:bracket_ipv6$", cmd, re.MULTILINE), (
        "login.cmd must define the :bracket_ipv6 subroutine."
    )
    assert 'set "BRACKETED=[%BRACKETED%]"' in cmd, (
        "login.cmd must add the IPv6 brackets when they are absent."
    )
    # The bracketing must run on BOTH roles and AFTER the classification, so the
    # GFMY_NOVNC_BIND environment path is normalised too and the loopback /
    # wildcard comparisons still see the spelling the operator typed.
    # Pinned by ROLE, not by a bare call count: since the token endpoint got its
    # own bind (GFMY_ONECLICK_BIND) there are more call sites, and a count would
    # have to be edited again for every further address role -- which is the kind
    # of edit that quietly drops a role instead of adding one. Each variable that
    # ends up in a docker port bind or in a printed URL must be assigned FROM the
    # bracketing subroutine's output.
    for role in ("NOVNC_BIND", "NOVNC_URL_HOST", "ONECLICK_BIND"):
        assert f'set "{role}=%BRACKETED%"' in cmd, (
            f"login.cmd must normalise {role} through :bracket_ipv6; an unbracketed "
            "IPv6 literal is printed as a broken URL and rejected by docker as a "
            "port bind."
        )
    assert cmd.count("call :bracket_ipv6 ") >= 2, (
        "login.cmd must bracket at least the bind AND the printed address (found "
        f"{cmd.count('call :bracket_ipv6 ')} call sites)."
    )
    classify_at = cmd.index('set "IS_LOOPBACK="')
    bracket_at = cmd.index("call :bracket_ipv6 ")
    assert classify_at < bracket_at, (
        "login.cmd must classify the bind BEFORE bracketing it; otherwise "
        "`--ip ::1` becomes `[::1]` and no longer matches the loopback test."
    )


def test_login_cmd_classifies_both_ipv6_spellings() -> None:
    """Every spelling that can reach a comparison must be covered by it.

    ``login.sh`` folds the spellings inside ``is_loopback_addr`` /
    ``is_wildcard_addr``; batch has no such helper, so each literal appears in
    the comparison chain and both spellings have to be listed explicitly. A
    missing one is a silent false all-clear (`--ip ::1` warning that the viewer
    is LAN-reachable) or a wildcard printed as a URL.
    """

    cmd = _read("login.cmd")
    for literal in ('"::1"', '"[::1]"'):
        assert f'if "%NOVNC_BIND%"=={literal} set "IS_LOOPBACK=1"' in cmd, (
            f"login.cmd must treat {literal} as a loopback bind."
        )
    for literal in ('"::"', '"[::]"', '"0.0.0.0"'):
        assert (
            f'if "%NOVNC_URL_HOST%"=={literal} set "NOVNC_URL_HOST=127.0.0.1"' in cmd
        ), f"login.cmd must replace the wildcard {literal} in the printed URL."


def test_login_cmd_validates_the_ip_value_like_login_sh() -> None:
    """``login.cmd --ip`` must reject a non-IP value, as ``login.sh`` does.

    Without it a mistyped host name, or a second option swallowed as the value,
    is printed as a working URL and then handed to docker as a port bind, where
    it fails much later with an opaque publish error. Both spellings (``--ip X``
    and ``--ip=X``) must be validated, and the failure must leave through the
    dedicated exit path rather than falling into the normal run.
    """

    cmd = _read("login.cmd")
    # Pinned per --ip PARSING BRANCH rather than by a global call count: the token
    # endpoint bind (GFMY_ONECLICK_BIND) runs through the same validator, so a
    # count would grow with every new address input and stop testing "both --ip
    # spellings" specifically. Each branch is read up to the next label.
    for branch in (":parse_ip\n", ":parse_ip_inline\n"):
        block = cmd.split("\n" + branch, 1)[1].split("\n:", 1)[0]
        assert "call :validate_ip " in block, (
            f"the {branch.strip()} branch must validate its value before using it."
        )
        assert "goto :ip_invalid" in block, (
            f"the {branch.strip()} branch must route a rejected value to :ip_invalid."
        )
    assert re.search(r"^:ip_invalid$", cmd, re.MULTILINE), (
        "login.cmd must define the :ip_invalid exit path."
    )
    # The exit path must unwind like every other one: popd, endlocal, exit /b 2.
    # Anchor on the LABEL DEFINITION at line start, not on the first `goto`.
    invalid_block = cmd.split("\n:ip_invalid\n", 1)[1].split("\n:", 1)[0]
    for token in ("popd", "endlocal", "exit /b 2"):
        assert token in invalid_block, (
            f":ip_invalid must {token} like the other early exits."
        )
    # An IP-literal allowlist, not a substring check: a host name must not pass.
    assert 'if "%CAND:~0,1%"=="-" exit /b 1' in cmd, (
        "login.cmd must reject a value that is actually the next option."
    )
    assert "if defined REST exit /b 1" in cmd, (
        "login.cmd must reject any character outside the IP-literal allowlist."
    )


def test_login_sh_rejects_unbalanced_ipv6_brackets(tmp_path: Path) -> None:
    """``is_ip_literal`` must not accept a spelling ``bracket_if_ipv6`` cannot fix.

    Accepting ``[::1`` would let the normaliser produce ``[[::1]``, so the
    promise "exactly one pair" in both comment blocks would be untrue and the
    value would still reach docker as a malformed port bind.

    The matrix also covers the empty-field class. ``is_ip_literal`` splits on a
    non-whitespace ``IFS`` and then counts the fields, but that splitter drops a
    single TRAILING separator and the counting loops skip empty fields, so the
    token result cannot testify to the shape of the raw value: ``1.2.3.4.``,
    ``:1:2:3:4:5:6:7:8`` and ``1:2:3:4:5:6:7:8:`` all produce the exact token
    count of a well-formed address. The structure is therefore checked BEFORE
    the split, and the positive controls below pin that the legal leading and
    trailing colons of ``::1`` / ``1::`` / ``::`` survive that guard.

    This runs the real script, so it is a behaviour test, not a re-implementation.
    """

    # Absolute: the script cd's to its own directory, and the run below sets a
    # cwd, so a repo-relative path would not resolve there.
    script = (DOCKER_LOGIN / "login.sh").resolve()

    # An ACCEPTED address does not end the script: it runs on to the final
    # `docker compose ... run --build --service-ports --rm`. On a machine with a
    # working Docker daemon (every GitHub runner) that really starts the login
    # container, which then waits for a sign-in that never comes. `capture_output`
    # plus a `timeout` does not save us there: the timeout kills only the direct
    # `bash` child, while the surviving `docker`/`docker-compose` grandchildren keep
    # the stdout pipe open, so the final `communicate()` blocks forever and the whole
    # pytest run hangs. Hence the same stub-`docker`-on-`PATH` harness that
    # `test_login_sh_behaviourally_selects_compose_files` already uses: the launcher
    # runs to completion, but its last step is a no-op echo.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text('#!/usr/bin/env bash\nprintf "DOCKER_ARGS: %s\\n" "$*"\n', "utf-8")
    stub.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    for value, expected_reject in (
        ("::1", False),
        ("[::1]", False),
        ("2001:db8::1", False),
        ("1:2:3:4:5:6:7:8", False),
        ("::", False),
        ("fe80::1", False),
        ("192.168.1.21", False),
        # Structural rules beyond "looks like IPv6": one compression run only,
        # at most four hex digits per group, eight groups unless compressed.
        ("1::2::3", True),
        ("12345::1", True),
        ("1:2:3:4:5:6:7:8:9", True),
        ("1:2:3:4:5:6:7", True),
        # A compression run stands for at least one omitted group, so eight
        # written-out groups next to it are already one too many.
        ("1:2:3:4:5:6:7:8::", True),
        ("::1:2:3:4:5:6:7:8", True),
        ("1:2:3:4:5:6:7::", False),
        ("[::1", True),
        ("]::1[", True),
        ("[[::1]]", True),
        # A lone colon passes a pure character allowlist, which is why the
        # structural checks exist: it would reach docker as a port bind.
        (":", True),
        (":::", True),
        ("abc:", True),
        ("1.2.3.0a", True),
        (".", True),
        ("not an ip", True),
        # Empty-field class (Codex P2 on login.cmd, same defect in login.sh).
        # An empty octet / group is invisible in the token result. Measured
        # against the pre-fix script, exactly THREE of the five below used to be
        # accepted and would have reached docker as a port bind: "1.2.3.4.",
        # ":1:2:3:4:5:6:7:8" and "1:2:3:4:5:6:7:8:". The other two were already
        # rejected -- a non-whitespace IFS does produce an empty LEADING field,
        # and ".." was caught by its own guard -- so they are kept here as
        # regression sentinels for that older guard, not as evidence for the new
        # one. Do not read this block as "all five were broken".
        ("1..2.3.4", True),
        (".1.2.3.4", True),
        ("1.2.3.4.", True),
        (":1:2:3:4:5:6:7:8", True),
        ("1:2:3:4:5:6:7:8:", True),
        # Neighbours of the same class, for the guard's boundaries.
        ("..", True),
        ("1.2.3.", True),
        (".1.2.3", True),
        (":1", True),
        ("1:", True),
        (":1:2", True),
        ("1:2:", True),
        # Positive controls: a leading or trailing colon IS legal when it is
        # part of the "::" run, and the plain forms must stay accepted. If the
        # new guard were written as a blanket "no leading/trailing colon", every
        # one of these four would flip to rejected.
        ("1::", False),
        ("::1", False),
        ("::", False),
        ("2001:db8::", False),
        ("1.2.3.4", False),
        ("0.0.0.0", False),
        ("127.0.0.1", False),
        ("255.255.255.255", False),
    ):
        proc = subprocess.run(
            ["bash", str(script), "--ip", value],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(DOCKER_LOGIN),
            env=env,
            timeout=60,
        )
        rejected = proc.returncode == 2 and "--ip needs an IP address" in proc.stderr
        assert rejected is expected_reject, (
            f"login.sh --ip {value!r}: rejected={rejected}, expected "
            f"{expected_reject} (rc={proc.returncode}, stderr={proc.stderr!r})"
        )


def test_login_cmd_validates_ipv6_structure_not_just_a_colon() -> None:
    """The batch validator must mirror the IPv6 half of ``is_ip_literal()``.

    A character allowlist alone accepts a lone ``:``, and ``:bracket_ipv6``
    leaves any value that already starts with ``[`` untouched, so ``[::1``
    would survive unbalanced. Both would be printed as a working URL and then
    handed to Compose as a port bind. ``login.cmd`` cannot be executed here (no
    Windows), so the structural checks are pinned by text.
    """

    cmd = _read("login.cmd")
    assert re.search(r"^:validate_ipv6$", cmd, re.MULTILINE), (
        "login.cmd must route colon-bearing values into a :validate_ipv6 block "
        "instead of accepting them outright."
    )
    assert 'if not "%INNER:~-1%"=="]" exit /b 1' in cmd, (
        "an opening bracket must require the matching closing bracket."
    )
    for residual in ('set "_T=%INNER:[=%"', 'set "_T=%INNER:]=%"'):
        assert residual in cmd, (
            f"login.cmd must reject residual brackets ({residual!r})."
        )
    assert 'set "_T=%INNER::::=%"' in cmd, "login.cmd must forbid a ':::' run."
    assert 'set "_T=%INNER:::=%"' in cmd, (
        "login.cmd must detect the '::' run before falling back to counting separators."
    )
    assert 'if "%INNER%"=="::" exit /b 0' in cmd, (
        "'::' is the one addressable-digit exception and must be spelled out."
    )


def test_login_cmd_mirrors_the_full_ipv6_structural_rules() -> None:
    """The batch validator must carry the same rules, not a weaker subset.

    ``login.sh`` is executed by the matrix above; ``login.cmd`` cannot be run
    here, so the three rules that go beyond "contains a colon" are pinned by
    text: a single compression run, group width, and group count.
    """

    cmd = _read("login.cmd")
    assert 'set "_TAIL=%INNER:*::=%"' in cmd, (
        "login.cmd must isolate the tail after the first '::' run to detect a "
        "second one."
    )
    assert re.search(r"^:v6_no_second_run$", cmd, re.MULTILINE), (
        "login.cmd must define the second-run rejection helper."
    )
    assert re.search(r"^:v6_group$", cmd, re.MULTILINE), (
        "login.cmd must define the per-group width helper."
    )
    assert 'if not "%_G:~4%"=="" exit /b 1' in cmd, (
        "a group wider than four hex digits must be rejected."
    )
    assert cmd.count('call :v6_group "%%') == 8, (
        "all eight possible groups must be width-checked (found "
        f"{cmd.count('call :v6_group ' + chr(34) + '%%')})."
    )
    assert 'if not "%%i"=="" exit /b 1' in cmd, "a ninth group must be rejected."
    assert 'if not defined _HAS_RUN if "%%h"=="" exit /b 1' in cmd, (
        "without a compression run, all eight groups must be present."
    )
    assert 'if defined _HAS_RUN if not "%%h"=="" exit /b 1' in cmd, (
        "with a compression run at most seven groups may be written out; "
        "checking only the ninth token accepts `1:2:3:4:5:6:7:8::`."
    )


def test_login_cmd_checks_empty_fields_before_splitting() -> None:
    """The batch validator must not derive structure from the token result.

    ``for /f`` collapses a run of delimiters into one and discards leading and
    trailing ones. ``.1.2.3.4``, ``1.2.3.4.`` and ``1..2.3.4`` therefore all
    tokenise into the same four clean octets, and ``:1:2:3:4:5:6:7:8`` /
    ``1:2:3:4:5:6:7:8:`` into eight clean groups, so every count test in
    ``:validate_ip`` / ``:validate_ipv6`` passes on a malformed value. The
    emptiness only exists on the raw string and has to be rejected there.

    Honest limits of this test: there is no cmd.exe and no wine in this
    environment, so ``login.cmd`` cannot be executed. What follows pins the
    PRESENCE of the guards and their position relative to the ``for /f`` they
    protect. It does NOT prove that they behave as intended on Windows; the
    executed proof of the same rules lives in
    ``test_login_sh_rejects_unbalanced_ipv6_brackets`` against the mirror
    ``login.sh``.

    That transfer holds only as far as the two files really accept the same
    set, and THAT is an intent pinned by text here, never an executed result:
    the shared allowlist above ``:validate_ipv6`` permits ``.`` for the IPv4
    branch, so the IPv6 branch has to narrow it away again to match the
    ``*[!0-9A-Fa-f:[]]*`` case in ``is_ip_literal``. The pin below exists
    because that narrowing was missing once, which let ``::1.`` through in the
    batch file while ``login.sh`` rejected it.
    """

    cmd = _read("login.cmd")

    # Parity of the character allowlist: the IPv6 branch must drop "." again,
    # and it must do so BEFORE the bracket handling, so that "[::1.]" cannot
    # reach the structural rules either.
    dot_guard = 'set "_T=%CAND:.=%"'
    assert dot_guard in cmd, (
        "the IPv6 branch must narrow the shared allowlist by rejecting '.', "
        "as the IPv6 case of is_ip_literal() in login.sh does; without it "
        "'::1.' passes here but is rejected there."
    )
    assert cmd.index(dot_guard) < cmd.index('set "INNER=%CAND%"'), (
        "the '.' rejection must run before the value is unbracketed into INNER."
    )

    # IPv4: no ".." run, no leading dot, no trailing dot.
    ipv4_guards = (
        'set "_T=%CAND:..=%"',
        'if "%CAND:~0,1%"=="." exit /b 1',
        'if "%CAND:~-1%"=="." exit /b 1',
    )
    for guard in ipv4_guards:
        assert guard in cmd, (
            f"login.cmd must reject an empty octet on the raw string ({guard!r})."
        )
    octet_split_at = cmd.index('for /f "tokens=1-5 delims=." ')
    for guard in ipv4_guards:
        assert cmd.index(guard) < octet_split_at, (
            f"{guard!r} must run BEFORE the octet split; after it the empty "
            "octet is already gone and the guard can never fire."
        )

    # IPv6: a leading or trailing ":" is legal only as part of a "::" run.
    ipv6_guards = (
        'if "%INNER:~0,1%"==":" if not "%INNER:~0,2%"=="::" exit /b 1',
        'if "%INNER:~-1%"==":" if not "%INNER:~-2%"=="::" exit /b 1',
    )
    for guard in ipv6_guards:
        assert guard in cmd, (
            f"login.cmd must reject an empty group at either end ({guard!r})."
        )
    group_split_at = cmd.index('for /f "tokens=1-9 delims=:" ')
    two_sep_split_at = cmd.index('for /f "tokens=1-3 delims=:" ')
    for guard in ipv6_guards:
        assert cmd.index(guard) < min(group_split_at, two_sep_split_at), (
            f"{guard!r} must run BEFORE every colon split, including the "
            "two-separator probe, which would otherwise keep reading an "
            "unreliable token count."
        )
    # The exemption for the "::" run has to hold in the FILE, not merely in the
    # literals pinned above: an additional unconditional rejection anywhere in
    # the block would leave every assertion above green while killing "::1",
    # "1::" and "::". Assert against `cmd`, never against `ipv6_guards` -- a
    # check that reads the test's own literals proves nothing about the code.
    for unconditional in (
        'if "%INNER:~0,1%"==":" exit /b 1',
        'if "%INNER:~-1%"==":" exit /b 1',
    ):
        assert unconditional not in cmd, (
            f"{unconditional!r} rejects a leading/trailing colon outright and "
            "would also reject '::1', '1::' and '::'; the guard must stay "
            "conditional on the '::' run."
        )


def test_cleartext_cleanup_reports_a_failed_removal() -> None:
    """Track C must not claim a removal it did not perform.

    The clear-text fallback prints the whole bundle and then deletes it. A
    silenced ``rm -f ... 2>/dev/null || true`` next to a message asserting the
    file "is now removed" lies whenever the delete fails (a readable file under
    a directory that is no longer writable), leaving the full credentials on
    disk with no warning.
    """

    entrypoint = _read("entrypoint.sh")
    assert 'rm -f "${_secrets_path}" 2>/dev/null || true' not in entrypoint, (
        "the clear-text cleanup must not force a successful exit status."
    )
    assert 'if rm -f "${_secrets_path}" 2>/dev/null; then' in entrypoint, (
        "the clear-text cleanup must branch on the actual removal result."
    )
    assert "could not remove" in entrypoint and "STILL on disk" in entrypoint, (
        "a failed removal must warn that the bundle is retained, naming the path."
    )
    # The unconditional past-tense claim must be gone from the preceding text.
    assert "The file is now removed." not in entrypoint, (
        "the message must not assert the removal before it has happened."
    )


def test_no_output_is_located_in_the_novnc_viewer() -> None:
    """Runtime output must be located where it is actually written.

    The container's own docs claimed twice that the pairing code and the
    ``GFMY_CLEARTEXT=1`` bundle can be read "inside the noVNC terminal"/"on the
    noVNC screen". Both are written to the entrypoint's stdout, i.e. the
    terminal running the launcher (and ``docker logs``), which is a different
    sink from the X display that noVNC renders: ``supervisord`` starts before
    ``GFMY_PAIRING_CODE`` exists, so no process under it inherits the value, and
    a shell opened inside the desktop is a fresh session that never sees this
    file descriptor.

    The guard pins the *predicate* ("output is located where it is written"),
    not the word "noVNC": performing the Google sign-in genuinely does happen in
    the noVNC window, and those true statements must survive. The three banned
    phrases below are location claims about *output* only.
    """

    entrypoint = _read("entrypoint.sh")
    readme = _read("README.md")

    for name, text in (("entrypoint.sh", entrypoint), ("README.md", readme)):
        for phrase in ("noVNC terminal", "noVNC screen", "inside the noVNC window"):
            assert phrase not in text, (
                f"{name} must not locate runtime output in the noVNC viewer "
                f"(found {phrase!r}); it is printed on the entrypoint's stdout."
            )

    # Positive counterpart: each of the two outputs names its real sink.
    assert "in the terminal that runs the" in entrypoint, (
        "the clear-text block must say it is printed in the launcher's terminal."
    )
    assert "docker logs" in entrypoint, (
        "the clear-text comment must name `docker logs` as the second sink, "
        "because that is what makes the block only as private as daemon access."
    )
    assert "in the terminal you started the launcher from" in readme, (
        "the README must tell the user where the pairing code and the "
        "clear-text block actually appear."
    )
    # The clear-text privacy note must not point at the wrong control knob:
    # GFMY_NOVNC_BIND governs port 7900 only and cannot limit stdout.
    cleartext_note = readme.split("### Terminal clear-text copy fallback")[-1]
    assert "does **not** limit it" in cleartext_note, (
        "the clear-text privacy note must state that GFMY_NOVNC_BIND does not "
        "constrain the terminal output."
    )


def test_no_english_source_locates_the_pairing_code_in_the_novnc_viewer() -> None:
    """Class-level guard: the mislocation must not come back anywhere else.

    ``test_no_output_is_located_in_the_novnc_viewer`` pins the two files where
    the claim was first found. It is the class, not those two files, that has to
    stay clean: the same sentence had already been copied into the config-flow
    docstrings, the translation source and the flow's own tests. Pinning only
    the reported spots is what let it survive there.

    Two complementary measurements, because a phrase sweep cannot cross
    languages:

    * phrase sweep over the English-language sources (Python, ``strings.json``,
      ``en.json``, tests);
    * structural check over *all* locale files -- no text describing the pairing
      code field may mention noVNC at all, in any language.

    True statements survive both: the Google sign-in genuinely happens in the
    noVNC window, and the step that asks for the address may say so.
    """

    import json as _json

    integration = Path("custom_components/googlefindmy")
    english_sources = [
        integration / "config_flow.py",
        integration / "container_login.py",
        integration / "strings.json",
        integration / "translations" / "en.json",
        Path("tests") / "test_config_flow_container_login.py",
        Path("tests") / "test_config_flow_initial_auth.py",
    ]
    banned = ("noVNC terminal", "noVNC screen", "inside the noVNC window")
    for source in english_sources:
        text = source.read_text(encoding="utf-8")
        for phrase in banned:
            assert phrase not in text, (
                f"{source} must not locate the pairing code or the clear-text "
                f"bundle in the noVNC viewer (found {phrase!r}); both are "
                "printed on the entrypoint's stdout."
            )
        for marker in ("read off the noVNC", "re-read it from the noVNC"):
            assert marker not in text, (
                f"{source} still tells the user to read the pairing code off "
                f"the noVNC session (found {marker!r})."
            )

    locales = [
        integration / "strings.json",
        *sorted((integration / "translations").glob("*.json")),
    ]
    assert len(locales) == 11, f"expected 11 text files, found {len(locales)}"
    for locale in locales:
        steps = _json.loads(locale.read_text(encoding="utf-8"))["config"]["step"]
        for step_name, block in steps.items():
            for section in ("data", "data_description"):
                for field, value in (block.get(section) or {}).items():
                    if "pairing" in field and isinstance(value, str):
                        assert "noVNC" not in value, (
                            f"{locale.name}: {step_name}.{section}.{field} still "
                            "locates the pairing code in the noVNC viewer."
                        )


def test_login_cmd_trims_trailing_blanks_from_every_inbound_switch() -> None:
    """`set VAR=1 && login.cmd` must not silently start the wrong mode.

    cmd.exe stores everything up to the `&&` INCLUDING the blank in front of
    it, so that documented one-liner leaves the value as ``"1 "``. Measured
    with a real cmd.exe: without a trim, `if "%GFMY_ONECLICK%"=="1"` is false
    and the launcher starts without the one-click overlay while still reporting
    success. The same environment is handed to `docker compose`, which
    interpolates it into the container, where `entrypoint.sh` compares
    ``GFMY_ONECLICK`` and ``GFMY_CLEARTEXT`` against ``"1"`` just as strictly.

    The expected set is therefore DERIVED from the compose files rather than
    restated here: every ``${GFMY_*}`` a compose file interpolates travels into
    the container and has to be trimmed, so adding one there without adding it
    to the launcher's list is exactly the regression this guards.
    """

    text = _read("login.cmd")
    lines = text.splitlines()

    from_compose = set(
        re.findall(
            r"\$\{(GFMY_[A-Z_]+)",
            _read("docker-compose.yml") + _read(ONECLICK_COMPOSE),
        )
    )
    assert from_compose, "no ${GFMY_*} found in the compose files"
    # Only this script reads it, so no compose file mentions it.
    expected = from_compose | {"GFMY_NOVNC_URL_HOST"}

    trim_lines = [
        i for i, line in enumerate(lines) if "call :trim_trailing_blanks" in line
    ]
    assert trim_lines, "login.cmd must call the trim routine"
    dispatch = min(trim_lines)
    covered = set(re.findall(r"GFMY_[A-Z_]+", lines[dispatch]))
    assert expected <= covered, (
        "login.cmd trims only part of the class; a user can set these and they "
        f"reach Compose or the container with a trailing blank: {sorted(expected - covered)}"
    )

    # The trim is worthless once a consumer has already read the value.
    for switch in sorted(expected):
        uses = [
            i
            for i, line in enumerate(lines)
            if f"%{switch}%" in line
            and not line.lstrip().startswith(("rem ", "REM ", "::"))
        ]
        if uses:
            assert dispatch < min(uses), (
                f"login.cmd reads {switch} at line {min(uses) + 1} before trimming "
                f"it at line {dispatch + 1}."
            )

    # The routine has to actually loop, and it has to loop on a BLANK: a body
    # that trims once leaves "1  " broken, and a body comparing against another
    # character never trims at all. Both mutations keep every other assertion
    # here green, so they are pinned explicitly.
    assert "\n:trim_trailing_blanks" in text, (
        "login.cmd calls :trim_trailing_blanks but never defines the label"
    )
    routine = text[text.index("\n:trim_trailing_blanks") :]
    assert ':~-1%"==" "' in routine, (
        "the trim routine must test the LAST character against a blank"
    )
    assert ":~0,-1%" in routine, "the trim routine must drop the last character"
    assert "goto :trim_trailing_blanks" in routine, (
        "the trim routine must recurse, otherwise it strips at most one blank"
    )


def test_login_cmd_forwards_novnc_url_host_before_compose_run() -> None:
    """Regression (Windows launcher, PR #1214 Codex follow-up).

    ``login.cmd`` normalises the browsable host into ``NOVNC_URL_HOST`` (including
    the ``--ip <ADDRESS>`` path) but must also re-export it as
    ``GFMY_NOVNC_URL_HOST`` *before* invoking ``docker compose``. Otherwise the
    compose interpolation ``${GFMY_NOVNC_URL_HOST:-localhost}`` falls back to
    ``localhost`` and a remote user is told to open an unreachable URL even though
    noVNC was published on the requested LAN address. ``login.sh`` exports it at
    the same point (see its ``export GFMY_NOVNC_URL_HOST`` line); this locks the
    Windows launcher to the same data path. Removing or moving the export after
    the compose run would restore the localhost fallback without this failing.
    """

    lines = _read("login.cmd").splitlines()

    export_idx = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().lower().startswith('set "gfmy_novnc_url_host=')
        ),
        -1,
    )
    assert export_idx >= 0, (
        "login.cmd must re-export the normalised host as GFMY_NOVNC_URL_HOST; "
        "without it the in-container noVNC URL falls back to localhost for a "
        "`login.cmd --ip <ADDRESS>` run."
    )
    assert "%NOVNC_URL_HOST%" in lines[export_idx], (
        "GFMY_NOVNC_URL_HOST must carry the normalised %NOVNC_URL_HOST% value, "
        "not a stale or empty literal."
    )

    run_idx = next(
        (
            index
            for index, line in enumerate(lines)
            if "docker compose" in line and "run --build" in line
        ),
        -1,
    )
    assert run_idx >= 0, "login.cmd must invoke `docker compose ... run --build`."
    assert export_idx < run_idx, (
        "GFMY_NOVNC_URL_HOST must be exported BEFORE `docker compose run`; moving "
        "it afterwards restores the localhost fallback this fix removed."
    )


@pytest.mark.parametrize(
    ("launcher", "assignment"),
    [
        ("login.sh", "export GFMY_NOVNC_URL_HOST="),
        ("login.cmd", 'set "GFMY_NOVNC_URL_HOST='),
    ],
)
def test_both_launchers_reexport_the_novnc_url_host(
    launcher: str, assignment: str
) -> None:
    """Both launchers must ACTUALLY assign GFMY_NOVNC_URL_HOST (not merely mention
    it in a comment) so the printed browsable URL matches across platforms; the
    Windows path regressed once (PR #1214). Matching the real assignment form
    keeps the check from passing on the ``rem``/``#`` explanations alone."""

    assert assignment.lower() in _read(launcher).lower(), (
        f"{launcher} must re-export GFMY_NOVNC_URL_HOST via `{assignment}...`; "
        "otherwise the in-container noVNC URL falls back to localhost."
    )


def _pip_install_commands(dockerfile: str) -> list[str]:
    """Return every ``pip install`` invocation in the Dockerfile as one line.

    Full-line comments are dropped, backslash line-continuations are joined, and
    inline ``#`` comments are stripped, so neither a multi-line
    ``RUN pip install ... \\`` nor a trailing ``# setuptools provided by base``
    comment can hide or fake what the effective command actually installs.
    """

    code = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    code = code.replace("\\\n", " ")
    commands: list[str] = []
    for raw in code.splitlines():
        line = re.sub(r"\s+#.*$", "", raw)  # strip inline comments
        if re.search(r"\bpip[0-9]*\b", line) and "install" in line:
            commands.append(line.strip())
    return commands


def test_dockerfile_installs_setuptools_for_the_distutils_shim() -> None:
    """The login image must install setuptools so undetected-chromedriver runs.

    undetected-chromedriver 3.5.5 (the current PyPI top, so a version bump is not
    available) still imports the stdlib ``distutils`` that Python 3.12+ removed
    (PEP 632). The ``selenium/standalone-chrome:latest`` base image is on Python
    3.14, so without ``setuptools`` re-providing ``distutils`` via its
    ``_distutils_hack`` the bare import fails (``ModuleNotFoundError: distutils``)
    and Chrome never starts. Dropping ``setuptools`` from the install step would
    pass every other guard here yet restore that failure, so pin it explicitly
    (Codex P1 on PR #1215).
    """

    dockerfile = _read("Dockerfile")
    pip_installs = _pip_install_commands(dockerfile)
    assert pip_installs, "Dockerfile must install Python dependencies via pip"
    assert any("requirements.txt" in cmd for cmd in pip_installs), (
        "Dockerfile must install the integration requirements via pip"
    )
    assert any(
        re.search(r"(?<![\w./-])setuptools(?![\w-])", cmd) for cmd in pip_installs
    ), (
        "the login image must install setuptools so undetected-chromedriver's "
        "distutils import keeps working on the Python 3.12+ base image "
        "(PEP 632 removed the stdlib distutils; setuptools re-provides it via "
        "_distutils_hack); do not drop it from the pip install step "
        "(Codex P1, PR #1215)."
    )


def test_pip_install_parser_ignores_inline_comments() -> None:
    """An inline ``# setuptools ...`` comment must not satisfy the setuptools
    guard.

    Without stripping inline comments, a Dockerfile such as
    ``RUN pip3 install -r /app/requirements.txt  # setuptools provided by base``
    would drop the real install yet still match the word ``setuptools`` in the
    comment, letting the ``distutils`` regression back in unnoticed. The parser
    strips inline comments so only the effective install arguments count
    (Codex P2, PR #1215).
    """

    faked = "RUN pip3 install -r /app/requirements.txt  # setuptools provided by base\n"
    commands = _pip_install_commands(faked)
    assert commands == ["RUN pip3 install -r /app/requirements.txt"]
    assert not any(
        re.search(r"(?<![\w./-])setuptools(?![\w-])", cmd) for cmd in commands
    ), "inline-comment setuptools must not count as an installed dependency"


def test_entrypoint_mints_password_before_supervisord_starts() -> None:
    """The per-run noVNC password is minted IN THE CONTAINER and exported before
    supervisord starts, so the base image's start-vnc.sh inherits it in time.

    The launchers only raise the GFMY_NOVNC_HARDEN verdict; moving the crypto here
    is what gives Windows (login.cmd) the same hardening without batch-side crypto.
    If the SE_VNC_PASSWORD export ever slipped below the ``supervisord`` launch,
    start-vnc.sh would have stored the public ``secret`` before the per-run
    password landed, silently defeating the hardening.
    """

    entrypoint = _read("entrypoint.sh")
    assert '"${GFMY_NOVNC_HARDEN:-}" = "1"' in entrypoint, (
        "entrypoint.sh must gate the password mint on the GFMY_NOVNC_HARDEN verdict"
    )
    export_idx = entrypoint.find("export SE_VNC_PASSWORD")
    launch_idx = entrypoint.find('bin/supervisord" --configuration')
    assert export_idx != -1, (
        "entrypoint.sh must export SE_VNC_PASSWORD for the hardened path"
    )
    assert launch_idx != -1, "entrypoint.sh must launch supervisord"
    assert export_idx < launch_idx, (
        "SE_VNC_PASSWORD must be exported BEFORE supervisord starts, or start-vnc.sh "
        "stores the public 'secret' before the per-run password lands."
    )


def test_entrypoint_password_charset_excludes_shell_hostile_symbols() -> None:
    """The in-container password alphabet stays letters + '. , = - _ + @' and never
    admits ``! ? % $``.

    Each excluded symbol has a SILENT-failure path in the code the value flows
    through (``%`` cmd.exe escape, ``!`` cmd.exe delayed expansion, ``?`` glob in
    the base image's unquoted ``x11vnc -storepasswd``, ``$`` shell metachar), so a
    well-meant charset widening would break logins without an error. Locking both
    the python3 alphabet and the /dev/urandom fallback here forces a conscious
    change if anyone edits them.
    """

    entrypoint = _read("entrypoint.sh")
    assert 'string.ascii_letters + ".,=-_+@"' in entrypoint, (
        "the python3 CSPRNG alphabet must stay the keyboard-safe set"
    )
    assert (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.,=-_+@" in entrypoint
    ), "the /dev/urandom fallback alphabet must match the keyboard-safe set"


def test_entrypoint_password_generator_fails_closed_without_csprng() -> None:
    """The password mint must FAIL CLOSED, never emit a weak credential.

    If both python3's ``secrets`` and ``/dev/urandom`` are unavailable, the
    hardened launch must abort rather than derive the LAN-exposed VNC password
    from Bash's non-cryptographic ``$RANDOM`` -- a predictable credential on a
    network-reachable viewer is worse than not starting. Codex flagged exactly
    this fail-open path; this guard keeps ``$RANDOM`` out of the mint and pins the
    abort branch so a later edit cannot silently reintroduce the weak fallback.
    """

    entrypoint = _read("entrypoint.sh")
    start = entrypoint.find('if [ "${GFMY_NOVNC_HARDEN:-}" = "1" ]; then')
    end = entrypoint.find("export SE_VNC_PASSWORD=", start)
    assert start != -1 and end != -1, "could not locate the password-mint block"
    mint = entrypoint[start:end]
    # Strip comment lines: the code must not USE $RANDOM, but a comment may name it
    # to document WHY it is excluded. Checking the raw text would flag that comment.
    mint_code = "\n".join(
        line for line in mint.splitlines() if not line.lstrip().startswith("#")
    )
    assert "RANDOM" not in mint_code, (
        "the password mint must not fall back to Bash's non-crypto $RANDOM"
    )
    assert "exit 1" in mint_code, (
        "the mint must fail closed (abort) when no CSPRNG byte is available"
    )


def test_compose_generates_password_in_container_not_from_host() -> None:
    """docker-compose.yml must NOT pass a VNC password from the host: the container
    mints it. Passing SE_VNC_PASSWORD from the host is exactly what re-introduces
    the empty-string override that overwrites the base image's ``secret`` default.
    Only the GFMY_NOVNC_HARDEN verdict (+ TLS + SAN host) crosses the boundary.
    """

    compose = yaml.safe_load(_read("docker-compose.yml"))
    env = compose["services"][LOGIN_SERVICE].get("environment", {})
    keys = set(env) if isinstance(env, dict) else {e.split("=", 1)[0] for e in env}
    assert "SE_VNC_PASSWORD" not in keys, (
        "SE_VNC_PASSWORD must not be a compose environment key; the container mints it"
    )
    assert "GOOGLEFINDMY_NOVNC_PASSWORD" not in keys, (
        "GOOGLEFINDMY_NOVNC_PASSWORD must not be passed from the host either"
    )
    assert "GFMY_NOVNC_HARDEN" in keys, (
        "the LAN-hardening verdict must cross into the container"
    )


def test_auth_flow_prompt_reads_password_from_env() -> None:
    """The [AuthFlow] sign-in banner must show the real per-run password, read from
    GOOGLEFINDMY_NOVNC_PASSWORD, not a hardcoded ``secret`` (which was always wrong
    for a hardened LAN login).
    """

    text = Path("custom_components/googlefindmy/Auth/auth_flow.py").read_text(
        encoding="utf-8"
    )
    assert "GOOGLEFINDMY_NOVNC_PASSWORD" in text, (
        "auth_flow.py must read the noVNC password from the environment so the "
        "sign-in banner shows the real per-run value"
    )


def test_login_sh_does_no_host_side_password_crypto() -> None:
    """login.sh must not generate or export the VNC password any more (that moved
    into entrypoint.sh); it only raises the GFMY_NOVNC_HARDEN verdict for a LAN bind.
    """

    text = _read("login.sh")
    assert "gen_password" not in text, (
        "password generation moved into the container; login.sh must not mint it"
    )
    assert "export SE_VNC_PASSWORD" not in text, (
        "login.sh must not export SE_VNC_PASSWORD; the container mints it"
    )
    assert "export GFMY_NOVNC_HARDEN=1" in text, (
        "login.sh must raise the hardening verdict for a non-loopback bind"
    )


def test_compose_forwards_the_bind_for_container_side_verdict() -> None:
    """The container must SEE the bind so it can derive the LAN-hardening verdict on
    the direct ``docker compose run`` path (which runs no launcher). Without
    GFMY_NOVNC_BIND in the container environment, a direct LAN bind would publish
    port 7900 with the fixed ``secret`` over plain HTTP -- the fixed-credential hole
    the launchers close. Codex flagged exactly this gap.
    """

    compose = yaml.safe_load(_read("docker-compose.yml"))
    env = compose["services"][LOGIN_SERVICE].get("environment", {})
    keys = set(env) if isinstance(env, dict) else {e.split("=", 1)[0] for e in env}
    assert "GFMY_NOVNC_BIND" in keys, (
        "GFMY_NOVNC_BIND must cross into the container so entrypoint.sh can derive "
        "the hardening verdict for a direct `docker compose run` LAN bind"
    )


def _run_derivation_block(env_overrides: dict[str, str]) -> dict[str, str]:
    """Extract the ACTUAL direct-Compose derivation block from entrypoint.sh and run
    it in isolation, then report the resulting verdict env.

    This exercises the production code slice, not a reimplementation of the
    classifier: a behavioural test that rebuilt the loopback/wildcard logic would
    only prove the rebuild, never the shipped block (the construct-vs-wiring trap).
    The block is self-contained (it runs before supervisord), so slicing it between
    its banner comment and the password-mint banner and feeding it a minimal env is
    faithful.
    """

    entrypoint = _read("entrypoint.sh")
    start = entrypoint.find('if [ "${GFMY_NOVNC_HARDEN:-}" != "1" ]; then')
    end = entrypoint.find("# --- Per-run noVNC password", start)
    assert start != -1 and end != -1, "could not extract the derivation block"
    block = entrypoint[start:end].rstrip()
    harness = (
        "set -e\n"
        + block
        + "\nprintf 'HARDEN=%s\\nTLS=%s\\nURL_HOST=%s\\nURL=%s\\n' "
        + '"${GFMY_NOVNC_HARDEN:-}" "${GFMY_NOVNC_TLS:-}" '
        + '"${GFMY_NOVNC_URL_HOST:-}" "${GOOGLEFINDMY_NOVNC_URL:-}"\n'
    )
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    # Mirror docker-compose.yml: it builds GOOGLEFINDMY_NOVNC_URL from URL_HOST,
    # defaulting the host to "localhost" when nothing set it on the host side.
    url_host_in = env_overrides.get("GFMY_NOVNC_URL_HOST", "") or "localhost"
    env["GOOGLEFINDMY_NOVNC_URL"] = f"http://{url_host_in}:7900"
    env.update(env_overrides)
    proc = subprocess.run(
        ["bash", "-c", harness],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"derivation block failed: {proc.stderr}"
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


# (bind-env, extra-env), expected HARDEN, expected TLS, expected URL_HOST
_DERIVATION_CASES = [
    # Loopback (or the compose empty-string default): never harden, clear stray TLS.
    ("compose-empty-bind", {"GFMY_NOVNC_BIND": ""}, "", "", ""),
    ("loopback-127-1", {"GFMY_NOVNC_BIND": "127.0.0.1"}, "", "", ""),
    ("loopback-127-x", {"GFMY_NOVNC_BIND": "127.0.0.5"}, "", "", ""),
    ("loopback-localhost", {"GFMY_NOVNC_BIND": "localhost"}, "", "", ""),
    ("loopback-v6", {"GFMY_NOVNC_BIND": "::1"}, "", "", ""),
    ("loopback-v6-bracketed", {"GFMY_NOVNC_BIND": "[::1]"}, "", "", ""),
    # A stray inherited TLS on a loopback run must be cleared, not honoured.
    (
        "loopback-clears-stray-tls",
        {"GFMY_NOVNC_BIND": "127.0.0.1", "GFMY_NOVNC_TLS": "1"},
        "",
        "",
        "",
    ),
    # Concrete LAN IP: harden + TLS, adopt the bind as SAN and browse host.
    ("lan-ip-192", {"GFMY_NOVNC_BIND": "192.168.1.21"}, "1", "1", "192.168.1.21"),
    ("lan-ip-10", {"GFMY_NOVNC_BIND": "10.0.0.5"}, "1", "1", "10.0.0.5"),
    # Wildcard: harden (password) but no SAN, so TLS stays off and URL_HOST empty.
    ("wildcard-v4", {"GFMY_NOVNC_BIND": "0.0.0.0"}, "1", "", ""),
    ("wildcard-v6", {"GFMY_NOVNC_BIND": "::"}, "1", "", ""),
    # Explicit TLS opt-out on a LAN bind: password stays, transport goes plain.
    (
        "lan-ip-opt-out",
        {"GFMY_NOVNC_BIND": "192.168.1.21", "GFMY_NOVNC_TLS": "0"},
        "1",
        "",
        "192.168.1.21",
    ),
    # Wildcard WITH an explicit URL_HOST: a SAN exists, so TLS is honoured.
    (
        "wildcard-explicit-host",
        {"GFMY_NOVNC_BIND": "0.0.0.0", "GFMY_NOVNC_URL_HOST": "192.168.1.21"},
        "1",
        "1",
        "192.168.1.21",
    ),
    # Concrete bracketed IPv6: the classifier strips the brackets, but URL_HOST and
    # the browse URL must KEEP them (the URL-correct form). Regression guard for the
    # bracket-strip-vs-keep split, the trickiest path.
    (
        "lan-ip-v6-bracketed",
        {"GFMY_NOVNC_BIND": "[fd00::5]"},
        "1",
        "1",
        "[fd00::5]",
    ),
    # Concrete bind IP WITH a preset URL_HOST: the -z guard must not overwrite the
    # explicit host nor rebuild the URL (Browse-Host = the operator's chosen host).
    (
        "lan-ip-preset-host",
        {"GFMY_NOVNC_BIND": "192.168.1.21", "GFMY_NOVNC_URL_HOST": "myhost.lan"},
        "1",
        "1",
        "myhost.lan",
    ),
    # Bracketed wildcard: login.sh lists "[::]" explicitly; entrypoint strips to "::".
    ("wildcard-v6-bracketed", {"GFMY_NOVNC_BIND": "[::]"}, "1", "", ""),
    # Hostname bind (GFMY_NOVNC_BIND allows names, unlike --ip): non-loopback, adopt.
    ("lan-hostname", {"GFMY_NOVNC_BIND": "myhost.lan"}, "1", "1", "myhost.lan"),
    # Literal "*" wildcard: pins the quoted-literal case semantics (never a catch-all).
    ("wildcard-star", {"GFMY_NOVNC_BIND": "*"}, "1", "", ""),
]


@pytest.mark.parametrize(
    "label,env_overrides,exp_harden,exp_tls,exp_url_host",
    _DERIVATION_CASES,
    ids=[c[0] for c in _DERIVATION_CASES],
)
def test_entrypoint_derivation_hardens_every_network_reachable_bind(
    label: str,
    env_overrides: dict[str, str],
    exp_harden: str,
    exp_tls: str,
    exp_url_host: str,
) -> None:
    """Behaviourally run the shipped derivation block over the bind type-space. The
    security invariant: EVERY network-reachable bind (concrete LAN IP or wildcard)
    ends with GFMY_NOVNC_HARDEN=1 (so the password is minted), and NO loopback bind
    does (so the plain ``secret`` default is preserved byte-for-byte). Codex flagged
    the direct-Compose path publishing 7900 with the fixed ``secret``; this pins the
    fix across the whole input space, not just one grep-able line.
    """

    out = _run_derivation_block(env_overrides)
    assert out["HARDEN"] == exp_harden, (
        f"{label}: HARDEN {out['HARDEN']!r} != {exp_harden!r}"
    )
    assert out["TLS"] == exp_tls, f"{label}: TLS {out['TLS']!r} != {exp_tls!r}"
    assert out["URL_HOST"] == exp_url_host, (
        f"{label}: URL_HOST {out['URL_HOST']!r} != {exp_url_host!r}"
    )
    # WICHTIG-2 regression pin: when a concrete bind IP is adopted as the SAN, the
    # printed browse URL must name that SAME host, never the compose "localhost"
    # default -- otherwise the banner promises https://localhost while the cert
    # certifies the LAN IP (a mismatched, broken promise).
    if exp_url_host and exp_url_host not in ("0.0.0.0", "::"):
        assert exp_url_host in out["URL"], (
            f"{label}: browse URL {out['URL']!r} must name the SAN host {exp_url_host!r}"
        )
        if exp_tls == "1":
            assert "localhost" not in out["URL"], (
                f"{label}: browse URL {out['URL']!r} must not keep the localhost default"
            )


def test_entrypoint_derivation_never_overrides_a_launcher_verdict() -> None:
    """When a launcher already raised GFMY_NOVNC_HARDEN=1, the container-side
    derivation must NOT run at all -- so an intentional launcher --no-tls (HARDEN=1,
    TLS="") survives, and a launcher LAN run is never downgraded. This pins the outer
    guard behaviourally, not by grep.
    """

    # Launcher --no-tls on a LAN bind: HARDEN=1, TLS empty. Must stay untouched even
    # though the bind itself is non-loopback (the derivation would otherwise raise TLS).
    out = _run_derivation_block(
        {
            "GFMY_NOVNC_HARDEN": "1",
            "GFMY_NOVNC_TLS": "",
            "GFMY_NOVNC_BIND": "192.168.1.21",
        }
    )
    assert out["HARDEN"] == "1", "launcher HARDEN verdict must be preserved"
    assert out["TLS"] == "", "launcher --no-tls (TLS empty) must not be overridden to 1"
