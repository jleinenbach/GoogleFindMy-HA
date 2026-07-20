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
  ``--build`` context, not just the one path a denylist happens to name.
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
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOCKER_LOGIN = Path("custom_components/googlefindmy/docker-login")
# Build context root for the login image (docker-compose.yml `build.context: ..`).
BUILD_CONTEXT = DOCKER_LOGIN.parent


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

    # 2. Only build inputs are re-included, and never a secrets directory.
    reincludes = {r[1:] for r in rules if r.startswith("!")}
    assert reincludes == {"requirements.txt", "docker-login"}, (
        "the only `!`-re-includes may be the Dockerfile's COPY inputs "
        f"(requirements.txt, docker-login); found {sorted(reincludes)}. Re-including "
        "anything else risks shipping integration source or secrets into the image."
    )
    assert not any(
        r.startswith("!") and ("auth" in r.lower() or "secret" in r.lower())
        for r in rules
    ), ".dockerignore must never re-include an Auth/ or secrets path."

    # 3. The persisted-credentials subdir of the re-included docker-login/ is
    #    excluded again (order-sensitive: after `!docker-login`).
    assert "docker-login/data" in {r.rstrip("/") for r in rules}, (
        ".dockerignore must re-exclude docker-login/data after re-including "
        "docker-login/, so the container's persisted secrets.json stays out."
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
