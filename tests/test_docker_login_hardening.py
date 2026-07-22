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
  stdin attached) rather than ``docker compose up``, or the one-time
  ``Press Enter`` prompt blocks forever (finding B).
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
  a first-login path: the interactive ``input("Press Enter")`` prompt can never
  proceed without a terminal, so the login must go through the stdin-attaching
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
  ``GFMY_ONECLICK=1`` (Codex P2 on PR #1211). When it IS published it stays pinned
  to ``127.0.0.1:7901:7901``: that host publish is the security boundary for an
  endpoint serving clear-text tokens, and there is deliberately no LAN opt-in for
  it (unlike ``GFMY_NOVNC_BIND`` for noVNC 7900).
* ``GFMY_ONECLICK`` and ``GFMY_CLEARTEXT`` are documented (compose + README) as
  INDEPENDENT switches, but the clear-text track hung off the one-click branch as
  an ``elif``, so with both set it was never evaluated -- in the documented lockout
  case the token server deliberately KEEPS ``secrets.json``, yet the requested
  clear-text fallback stayed silent. The tracks are now two separate ``if``s, each
  re-testing ``-f "${_secrets_path}"`` so an acked/TTL-consumed (deleted) bundle
  still prints nothing (Codex P2 on PR #1211).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
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
    """Launchers must use interactive ``compose run`` so the Enter prompt reaches Python."""

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


def test_oneclick_overlay_publishes_token_port_on_host_loopback_only() -> None:
    """The overlay must publish 7901 exclusively as ``127.0.0.1:7901:7901``.

    The endpoint serves the freshly minted tokens in the clear, so the host-side
    loopback publish IS the security boundary (the in-container bind is ``0.0.0.0``
    on purpose, because Docker's bridge DNATs the published port onto eth0 rather
    than onto container loopback). A wildcard publish (``0.0.0.0:7901:7901``) or a
    bare ``7901:7901`` would put clear-text credentials on the LAN.
    """

    overlay = DOCKER_LOGIN / ONECLICK_COMPOSE
    assert overlay.is_file(), (
        f"{ONECLICK_COMPOSE} must exist next to docker-compose.yml: it carries the "
        "opt-in one-click token-port publish that the launchers add on demand."
    )

    ports = _compose_service_ports(ONECLICK_COMPOSE)
    token_ports = [p for p in ports if "7901" in p]
    assert token_ports == ["127.0.0.1:7901:7901"], (
        f"{ONECLICK_COMPOSE} must publish the token port exactly as "
        f"'127.0.0.1:7901:7901' (found {ports!r}). Anything else exposes clear-text "
        "tokens beyond host loopback."
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
def test_no_lan_opt_in_exists_for_the_token_port(compose_file: str) -> None:
    """No compose file may offer a LAN bind for 7901, not even via a variable.

    noVNC deliberately has one (``GFMY_NOVNC_BIND``); the token port deliberately
    has none, because it hands out credentials in the clear. This guard travels
    with the publish: it now covers the overlay as strictly as it covered the base
    file before the split.
    """

    for entry in _compose_service_ports(compose_file):
        if "7901" not in entry:
            continue
        assert entry == "127.0.0.1:7901:7901", (
            f"{compose_file} publishes the token port as {entry!r}. It must be the "
            "hard-coded loopback publish '127.0.0.1:7901:7901' -- no wildcard, no "
            "bare mapping, and no variable-driven host bind (there is no LAN opt-in "
            "for this port by design; use an SSH tunnel instead)."
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
    gate = next(
        i for i, line in enumerate(lines) if '"${GFMY_ONECLICK:-}" = "1"' in line
    )
    add = next(i for i, line in enumerate(lines) if line in adding_lines)
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
    }, (
        "the only `!`-re-includes may be the Dockerfile's COPY inputs "
        "(requirements.txt, docker-login/entrypoint.sh) plus `docker-login` for "
        f"traversal; found {sorted(reincludes)}. Re-including anything else -- or the "
        "directory as a whole without re-excluding its contents -- risks shipping "
        "integration source or secrets into the image."
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


def test_entrypoint_forwards_signals_to_cli_child() -> None:
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
    assert "CLI_PID=$!" in entrypoint, (
        "the entrypoint must capture the CLI child PID (CLI_PID=$!) so signals can "
        "be forwarded to it and its exit status waited on."
    )

    # 2) The entrypoint waits on that specific child (not a bare `wait`).
    assert 'wait "${CLI_PID}"' in entrypoint, (
        "the entrypoint must wait on the CLI child PID so its real exit status "
        "propagates to the EXIT cleanup."
    )

    # 3) The TERM/INT traps forward to the child instead of exiting immediately.
    assert "trap 'on_signal TERM 143' TERM" in entrypoint
    assert "trap 'on_signal INT 130' INT" in entrypoint
    assert "trap 'exit 143' TERM" not in entrypoint, (
        "a bare `exit` on SIGTERM does not reach the foreground child; the trap must "
        "forward the signal to CLI_PID (on_signal)."
    )
    assert "trap 'exit 130' INT" not in entrypoint

    # 4) on_signal actually relays the signal to the running child.
    assert 'kill -s "${sig}" "${CLI_PID}"' in entrypoint, (
        "on_signal must relay the received signal to the CLI child via "
        "kill -s <sig> CLI_PID."
    )


def test_entrypoint_preserves_stdin_and_sigint_for_background_child() -> None:
    """Backgrounding the CLI must not break the first-run login prompt or Ctrl-C.

    A non-interactive bash script with job control off applies two disruptive
    defaults to an async (``&``) child, both proven empirically:

    * its stdin is pointed at ``/dev/null`` unless an explicit redirection overrides
      it -- so ``main.py``'s interactive ``input("Press Enter")`` prompt would hit EOF
      and abort the login (Codex: "Keep stdin attached when backgrounding the CLI");
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
      first-run ``input("Press Enter")`` prompt does not hit EOF; and
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
            "CLI_PID=$!\n"
            'wait "${CLI_PID}"\n'
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
    first-run ``input("Press Enter")`` prompt can never proceed. Telling users to "keep
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
    chained = chained.replace("|| true\nfi\n", "|| true\n", 1)
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
    proc = subprocess.run(
        ["bash", "-c", "set -e\n_rc=0\n" + script],
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

    return [
        line
        for line in _read("entrypoint.sh").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


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

    The token endpoint (7901) has no such opt-in on purpose: its loopback
    publish is pinned statically in ``docker-compose.oneclick.yml`` and guarded
    by ``test_no_lan_opt_in_exists_for_the_token_port`` above. There is no
    runtime check in either launcher, and this test must not imply one.
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
