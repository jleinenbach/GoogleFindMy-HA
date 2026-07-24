#!/usr/bin/env bash
#
# Entrypoint for the GoogleFindMy-HA login container.
#
# Boots the virtual-display / noVNC stack from the selenium/standalone-chrome
# base image, waits for the X display, then runs the integration's *own*
# standalone CLI (custom_components/googlefindmy/main.py) in the foreground.
#
# On the first run (no cached tokens) main.py launches the Chrome login flow;
# once a usable secrets.json exists it returns early and just lists devices —
# no browser step. Pass extra flags via the GFMY_ARGS environment variable,
# e.g. GFMY_ARGS="--reauth" to force a fresh login, or "--debug" for verbose
# bootstrap/FCM logging.
set -e

"${VENV_PATH}/bin/supervisord" --configuration /etc/supervisord.conf &
SUPERVISOR_PID=$!

# cleanup runs on EVERY exit path: normal completion, a nonzero main.py exit
# (Chrome/login/network failure aborting under `set -e`), or SIGTERM/SIGINT. It
# performs the ownership handoff FIRST, so a produced (even partial) owner-only
# secrets.json is always returned to the host user. Otherwise, after the pre-run
# chown of /data to the container UID below, a failed run would leave the host
# unable to read or delete the file. It then stops supervisor. The guard keeps
# it idempotent so the EXIT trap and a signal-driven exit cannot double-run it.
_cleaned=0
# PID of the child currently being waited on, or "" when none is running. Every
# child this entrypoint WAITS ON goes through this single slot (supervisord is
# backgrounded separately and torn down in cleanup, see
# wait_for_tracked_child below), so on_signal has exactly one place to check. In
# the gaps between children (waiting for the X display, between main.py and the
# token server) the slot is empty on purpose and on_signal exits directly.
CHILD_PID=""
# Set to the conventional 128+signal code once a terminating signal has been
# relayed to a child. Needed because a child may absorb the signal and still exit
# 0 (main.py does exactly that on SIGINT), leaving no trace in the wait status.
# Read by exit_if_terminating below, which every wait is followed by.
_terminating=""
function cleanup {
  [ "${_cleaned}" = 1 ] && return
  _cleaned=1

  # Ownership handoff: main.py writes secrets.json 0600 owned by the container
  # user. Hand the produced credentials back to the *host* user (UID/GID passed
  # by the launcher) while KEEPING owner-only 0600, so you can read/copy the file
  # for the Home Assistant import and no other local account can (unlike a
  # world-readable 0644 relaxation). Without a host UID (e.g. a manual
  # `docker compose up`) the file simply stays container-owned.
  _secrets_path="${GOOGLEFINDMY_SECRETS_PATH:-}"
  _data_dir="$(dirname "${_secrets_path:-/data/secrets.json}")"
  if [ -n "${GFMY_HOST_UID:-}" ] && [ -d "${_data_dir}" ]; then
    sudo chown -R "${GFMY_HOST_UID}:${GFMY_HOST_GID:-${GFMY_HOST_UID}}" "${_data_dir}" 2>/dev/null || true
    sudo chmod 0700 "${_data_dir}" 2>/dev/null || true
    if [ -f "${_secrets_path}" ]; then
      sudo chmod 0600 "${_secrets_path}" 2>/dev/null || true
    fi
  fi

  kill -s SIGTERM "${SUPERVISOR_PID}" 2>/dev/null || true
  wait "${SUPERVISOR_PID}" 2>/dev/null || true
}
# On a terminating signal, FORWARD it to the tracked child instead of exiting
# straight away: every long-running child runs in the background so bash can react
# to the signal at once and relay it. A foreground child would make bash defer these
# traps until it exits on its own, so `docker stop` would escalate to SIGKILL and the
# EXIT cleanup (ownership handoff + supervisor shutdown) would never run. Between two
# tracked children (still waiting for the X display, or between main.py and the token
# server) there is nothing to forward to, so fall back to exiting with the
# conventional 128+signal code. The single EXIT trap runs the handoff+shutdown exactly
# once, on every path.
function on_signal {
  local sig="$1" code="$2"
  if [ -n "${CHILD_PID}" ] && kill -0 "${CHILD_PID}" 2>/dev/null; then
    _terminating="${code}"
    kill -s "${sig}" "${CHILD_PID}" 2>/dev/null || true
  else
    exit "${code}"
  fi
}

# Wait for the child in CHILD_PID and return ITS exit status, without tripping
# `set -e` in the caller (call as `wait_for_tracked_child || rc=$?`). `wait` returns
# >128 when a forwarded signal interrupts the wait before the child is reaped, so
# re-wait until the child has actually exited. Clearing the slot afterwards makes
# on_signal fall back to a direct `exit` in the gaps between children: `kill -0`
# would already fail on a reaped PID, but that PID number can since have been
# recycled by an unrelated process, and signalling THAT is what the reset prevents.
function wait_for_tracked_child {
  local rc=0
  # Defensive, for a future call site: today both callers assign CHILD_PID=$!
  # immediately before, so the slot is never empty here. Were it ever called with
  # an empty slot, `wait ""` would print a usage error and return 1, which the
  # `-eq 0` gates below would read as a failed login.
  [ -n "${CHILD_PID}" ] || return 0
  wait "${CHILD_PID}" || rc=$?
  while [ "${rc}" -gt 128 ] && kill -0 "${CHILD_PID}" 2>/dev/null; do
    rc=0
    wait "${CHILD_PID}" || rc=$?
  done
  CHILD_PID=""
  return "${rc}"
}

# Call after EVERY wait_for_tracked_child, passing that child's status. A tracked
# child dies on the relayed signal and its wait() returns NORMALLY, so the status
# alone cannot tell "finished" from "we are shutting down" -- main.py even catches
# KeyboardInterrupt and returns 0 (main.py: `except KeyboardInterrupt: print`), and
# a server that shuts down gracefully may do the same. Without this the run would
# continue into the post-login handoff after a `docker stop`: starting a token
# server nobody can still reach, or printing the credential bundle into the
# container log, and reporting success either way.
#
# Two independent shutdown paths, because neither status nor marker covers both:
# the marker is set only when WE relayed the signal, and the >128 status appears
# only when the child died FROM it (Ctrl-C on a TTY reaches the whole process
# group, so the child can be gone before we ever relay).
function exit_if_terminating {
  local rc="${1:-0}"
  if [ -n "${_terminating}" ]; then
    exit "${_terminating}"
  fi
  if [ "${rc}" -gt 128 ]; then
    exit "${rc}"
  fi
}
trap cleanup EXIT
trap 'on_signal TERM 143' TERM
trap 'on_signal INT 130' INT

echo "[entrypoint] Waiting for X display ${DISPLAY} to come up..."
for i in $(seq 1 30); do
  if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    echo "[entrypoint] Display ready."
    break
  fi
  sleep 1
done

echo "[entrypoint] Open http://localhost:7900 (password: secret) in your browser to see/drive Chrome."

# /data is a host bind mount whose owner/mode we do not control. Take ownership
# for the container user for the duration of the run so main.py's atomic write of
# secrets.json succeeds, without requiring a world-writable host directory.
# seluser has passwordless sudo in the selenium base image.
sudo chown -R "$(id -u):$(id -g)" /data 2>/dev/null || true

# Run the integration's own CLI from the read-only FLAT code mount (/app/gfmy).
# Running the script by path (not `-m custom_components.googlefindmy.main`) keeps
# main.py in its standalone layout: it stubs homeassistant.* and never imports
# the package __init__/config_flow, which require voluptuous + a real Home
# Assistant install (neither is present in this image on purpose). secrets.json
# goes to the writable /data volume via GOOGLEFINDMY_SECRETS_PATH.
cd /app/gfmy
# Start the CLI in the BACKGROUND and wait on it, so a SIGTERM/SIGINT reaches bash
# immediately and on_signal can relay it to the child (a foreground child defers the
# traps until it exits -> docker escalates to SIGKILL -> no cleanup).
#
# Backgrounding in a non-interactive script (job control off) has two side effects we
# must undo, or the first-run login breaks:
#   1. stdin: bash points an async child's stdin at /dev/null unless an explicit
#      redirection overrides it. main.py's interactive `input("Press Enter")` prompt
#      would then hit EOF and abort the login. The `<&0` at the async boundary (NOT
#      inside the subshell, where fd 0 is already /dev/null) restores the entrypoint's
#      terminal stdin (which `docker compose run` attaches).
#   2. SIGINT/SIGQUIT: bash hard-ignores (SIG_IGN) these in an async child; Python
#      inherits SIG_IGN and then declines to install its KeyboardInterrupt handler, so
#      a relayed SIGINT (Ctrl-C on `docker compose run`) is dropped and the re-wait
#      loop hangs. `trap - INT QUIT` in the wrapper restores the default disposition
#      before exec, so Python installs its normal handlers again.
# `exec` makes CHILD_PID the Python process itself, so on_signal's relay reaches it.
# shellcheck disable=SC2086 -- GFMY_ARGS is an intentional word-split flag list.
( trap - INT QUIT; exec python3 main.py ${GFMY_ARGS:-} ) <&0 &
CHILD_PID=$!

# Capture the child's status without tripping `set -e`, then propagate it via the
# final `exit` so the EXIT trap runs cleanup() (ownership handoff to the host user,
# then supervisor shutdown) exactly once on every path: success, nonzero exit, or
# termination. The signal-interrupted re-wait lives in wait_for_tracked_child.
_rc=0
wait_for_tracked_child || _rc=$?
exit_if_terminating "${_rc}"

# --------------------------------------------------------------------------
# Post-login handoff tracks (only after a successful main.py run; unset -> the
# historical behaviour is completely unchanged).
#
# GFMY_ONECLICK and GFMY_CLEARTEXT are INDEPENDENT switches (as documented in
# docker-compose.yml and README.md), so the two blocks below are two separate
# `if`s evaluated in sequence, never an if/elif chain: with both set, Track C
# has to stay reachable after the token server returned. Each block re-tests
# `-f "${_secrets_path}"`, which is what keeps the sequence honest -- the server
# consumes (deletes) the file on ack and on TTL, so Track C only ever prints a
# bundle that is still there -- chiefly the lockout case, and likewise anything
# else that leaves the file behind (a failed delete, for instance) -- never an
# empty block.
#
# These run BLOCKING (the children themselves are backgrounded and tracked, see
# below) *before* the final `exit`, so the EXIT trap's
# cleanup (ownership handoff of /data + supervisor shutdown) fires strictly
# AFTER the handoff is done. That is the BLOCKING-1 lifecycle fix: a One-Click
# token server that must outlive main.py cannot hang off the EXIT trap, so it
# runs here, and cleanup's /data ownership handoff is naturally deferred until
# it returns (ack-delete or TTL). The `secrets.json` the server reads still
# exists at this point (main.py wrote it; cleanup has not run yet).
# --------------------------------------------------------------------------
_secrets_path="${GOOGLEFINDMY_SECRETS_PATH:-/data/secrets.json}"

if [ "${_rc}" -eq 0 ] && [ "${GFMY_ONECLICK:-}" = "1" ] && [ -f "${_secrets_path}" ]; then
  # Track B: hand the freshly minted bundle to Home Assistant over a one-shot,
  # nonce-authenticated, loopback-only endpoint (see token_server.py). The
  # pairing code is generated at RUNTIME (never a compose default) and printed
  # prominently; only the code is shown, never the token/bundle.
  GFMY_PAIRING_CODE="$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')"
  export GFMY_PAIRING_CODE GOOGLEFINDMY_SECRETS_PATH="${_secrets_path}"
  echo ""
  echo "=================================================================="
  echo "[entrypoint] ONE-CLICK login ready. In Home Assistant choose the"
  echo "[entrypoint] 'Container login' auth method and enter:"
  echo "[entrypoint]     host: 127.0.0.1   port: 7901"
  echo "[entrypoint]     pairing code: ${GFMY_PAIRING_CODE}"
  echo "[entrypoint] (From another machine, tunnel: ssh -L 7901:127.0.0.1:7901 <host>)"
  echo "[entrypoint]"
  echo "[entrypoint] If Home Assistant cannot reach the port, the host publish is"
  echo "[entrypoint] missing: 7901 is an OPT-IN overlay so that a busy port never"
  echo "[entrypoint] blocks the file/cleartext tracks. The launcher adds it for you"
  echo "[entrypoint] (GFMY_ONECLICK=1 bash login.sh); a manual run needs it spelled out:"
  echo "[entrypoint]   docker compose -f docker-compose.yml -f docker-compose.oneclick.yml \\"
  echo "[entrypoint]     run --build --service-ports --rm googlefindmy-login"
  echo "=================================================================="
  echo ""
  # Backgrounded and tracked, for the SAME reason main.py is (see the launch
  # comment above): with a FOREGROUND server bash would defer its TERM/INT traps
  # for up to the full TTL, so a `docker stop` during the wait would hit SIGKILL
  # after the (default 10 s) grace period and the EXIT cleanup would never run --
  # leaving the 0600 bundle owned by the container UID and supervisor unstopped.
  # `trap - INT QUIT` restores Python's default SIGINT disposition in the async
  # child so token_server.py's KeyboardInterrupt path stays reachable; no `<&0`
  # here, the server never reads stdin.
  #
  # This blocks until one of four things happens. In the first three we fall
  # through to the final exit -> EXIT trap cleanup (ownership handoff +
  # supervisor stop):
  #   - Home Assistant acks the handoff  -> secrets.json is deleted (consumed),
  #   - the TTL elapses without an ack   -> secrets.json is deleted (fallback),
  #   - the pairing code is locked out   -> the endpoint closes but secrets.json
  #     is KEPT on purpose, so no login has to be repeated: the file handoff
  #     (Track A) still works, and if GFMY_CLEARTEXT is also set the next block
  #     prints the bundle. Note that the two are alternatives, not cumulative:
  #     Track C is ephemeral by contract and removes the file after printing it,
  #     so with both switches the clear-text output REPLACES the file handoff.
  # The fourth is a terminating signal, and it does NOT fall through: see the
  # check right after the wait. secrets.json is then left on disk unless the
  # server had already consumed it, which is the safe
  # direction -- no login has to be repeated, and the EXIT trap still hands the
  # file's ownership back to the host user.
  ( trap - INT QUIT; exec python3 /app/gfmy/docker-login/token_server.py ) &
  CHILD_PID=$!
  _srv_rc=0
  wait_for_tracked_child || _srv_rc=$?
  # The server dying because we are shutting down is NOT "the server finished":
  # without this the clear-text track below would dump the whole bundle into the
  # container log on a `docker stop`. token_server.py installs no SIGTERM handler,
  # so the file it would otherwise have consumed is still there and that `-f` test
  # would pass.
  exit_if_terminating "${_srv_rc}"
fi

# Deliberately a fresh `if`, not an `elif` on the block above: the switches are
# independent, so with GFMY_ONECLICK=1 *and* GFMY_CLEARTEXT=1 this is the
# promised fallback for the lockout case, where the server left secrets.json in
# place on purpose. The `-f` test is re-evaluated AFTER the server returned, so
# an acked or TTL-expired (i.e. deleted) bundle prints nothing at all. The
# clear-text block exists exactly once and serves both entry paths.
if [ "${_rc}" -eq 0 ] && [ "${GFMY_CLEARTEXT:-}" = "1" ] && [ -f "${_secrets_path}" ]; then
  # Track C: no port is opened. Print the full secrets.json in a clearly
  # delimited block on THIS script's stdout, i.e. in the terminal that runs the
  # launcher (or in `docker logs`). That is a different sink from the noVNC
  # viewer, which renders the X display served by supervisord and never sees
  # this file descriptor; a terminal opened inside the desktop is a fresh shell
  # under supervisord, not this output stream. Select and copy the block there
  # and paste it into Home Assistant's secrets.json field. The file is ephemeral
  # and removed right after display so nothing lingers on disk.
  echo ""
  echo "=================== BEGIN secrets.json (copy) ===================="
  cat "${_secrets_path}"
  echo ""
  echo "==================== END secrets.json (copy) ====================="
  echo "[entrypoint] Select the block above, copy it, and paste it into the"
  echo "[entrypoint] Home Assistant 'secrets.json' field."
  echo ""
  # Report the real outcome instead of asserting it. A silenced `rm` claimed the
  # bundle was gone even when it was not (a readable file under a directory that
  # is no longer writable), leaving the full credentials on disk with no warning
  # -- the opposite of what Track C promises.
  if rm -f "${_secrets_path}" 2>/dev/null; then
    echo "[entrypoint] The file has been removed."
  else
    echo "[entrypoint] WARNING: could not remove ${_secrets_path}." >&2
    echo "[entrypoint] The full credential bundle is STILL on disk at that path." >&2
    echo "[entrypoint] Delete it yourself before leaving this host unattended." >&2
  fi
fi

exit "${_rc}"
