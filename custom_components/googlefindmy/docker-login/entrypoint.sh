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
CLI_PID=""
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
# On a terminating signal, FORWARD it to the CLI child (started below) instead of
# exiting straight away: main.py runs in the background so bash can react to the
# signal at once and relay it. A foreground child would make bash defer these traps
# until it exits on its own, so `docker stop` would escalate to SIGKILL and the
# EXIT cleanup (ownership handoff + supervisor shutdown) would never run. Before the
# child exists (still waiting for the X display) there is nothing to forward to, so
# fall back to exiting with the conventional 128+signal code. The single EXIT trap
# runs the handoff+shutdown exactly once, on every path.
function on_signal {
  local sig="$1" code="$2"
  if [ -n "${CLI_PID}" ] && kill -0 "${CLI_PID}" 2>/dev/null; then
    kill -s "${sig}" "${CLI_PID}" 2>/dev/null || true
  else
    exit "${code}"
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
# `exec` makes CLI_PID the Python process itself, so on_signal's relay reaches it.
# shellcheck disable=SC2086 -- GFMY_ARGS is an intentional word-split flag list.
( trap - INT QUIT; exec python3 main.py ${GFMY_ARGS:-} ) <&0 &
CLI_PID=$!

# Capture the child's status without tripping `set -e`. `wait` returns >128 when a
# forwarded signal interrupts it before the child is reaped; re-wait until the child
# has actually exited, then propagate its code via `exit` so the EXIT trap runs
# cleanup() (ownership handoff to the host user, then supervisor shutdown) exactly
# once on every path: success, nonzero exit, or termination.
_rc=0
wait "${CLI_PID}" || _rc=$?
while [ "${_rc}" -gt 128 ] && kill -0 "${CLI_PID}" 2>/dev/null; do
  _rc=0
  wait "${CLI_PID}" || _rc=$?
done

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
# These run in the FOREGROUND *before* the final `exit`, so the EXIT trap's
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
  echo "[entrypoint] (GFMY_ONECLICK=1 ./login.sh); a manual run needs it spelled out:"
  echo "[entrypoint]   docker compose -f docker-compose.yml -f docker-compose.oneclick.yml \\"
  echo "[entrypoint]     run --build --service-ports --rm googlefindmy-login"
  echo "=================================================================="
  echo ""
  # Foreground: this blocks until one of three things happens, then returns and
  # we fall through to the final exit -> EXIT trap cleanup (ownership handoff +
  # supervisor stop):
  #   - Home Assistant acks the handoff  -> secrets.json is deleted (consumed),
  #   - the TTL elapses without an ack   -> secrets.json is deleted (fallback),
  #   - the pairing code is locked out   -> the endpoint closes but secrets.json
  #     is KEPT on purpose, so no login has to be repeated: the file handoff
  #     (Track A) still works, and if GFMY_CLEARTEXT is also set the next block
  #     prints the bundle. Note that the two are alternatives, not cumulative:
  #     Track C is ephemeral by contract and removes the file after printing it,
  #     so with both switches the clear-text output REPLACES the file handoff.
  python3 /app/gfmy/docker-login/token_server.py || true
fi

# Deliberately a fresh `if`, not an `elif` on the block above: the switches are
# independent, so with GFMY_ONECLICK=1 *and* GFMY_CLEARTEXT=1 this is the
# promised fallback for the lockout case, where the server left secrets.json in
# place on purpose. The `-f` test is re-evaluated AFTER the server returned, so
# an acked or TTL-expired (i.e. deleted) bundle prints nothing at all. The
# clear-text block exists exactly once and serves both entry paths.
if [ "${_rc}" -eq 0 ] && [ "${GFMY_CLEARTEXT:-}" = "1" ] && [ -f "${_secrets_path}" ]; then
  # Track C: no port is opened. Print the full secrets.json in a clearly
  # delimited block so the user can SELECT + COPY it inside the noVNC terminal
  # and paste it into Home Assistant's secrets.json field. The file is ephemeral
  # and removed right after display so nothing lingers on disk.
  echo ""
  echo "=================== BEGIN secrets.json (copy) ===================="
  cat "${_secrets_path}"
  echo ""
  echo "==================== END secrets.json (copy) ====================="
  echo "[entrypoint] Select the block above, copy it, and paste it into the"
  echo "[entrypoint] Home Assistant 'secrets.json' field. The file is now removed."
  echo ""
  rm -f "${_secrets_path}" 2>/dev/null || true
fi

exit "${_rc}"
