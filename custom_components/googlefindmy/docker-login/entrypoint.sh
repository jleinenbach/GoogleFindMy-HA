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

function shutdown {
  kill -s SIGTERM "${SUPERVISOR_PID}" 2>/dev/null || true
  wait "${SUPERVISOR_PID}" 2>/dev/null || true
}
trap shutdown SIGTERM SIGINT

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
# shellcheck disable=SC2086 -- GFMY_ARGS is an intentional word-split flag list.
python3 main.py ${GFMY_ARGS:-}

# Ownership handoff: main.py writes secrets.json 0600 owned by the container user.
# Hand the produced credentials back to the *host* user (UID/GID passed by the
# launcher) while KEEPING owner-only 0600. The host user can then read/copy the
# file for the Home Assistant import, and no other local account can read it —
# unlike a world-readable 0644 relaxation. If the launcher did not pass a host UID
# (e.g. a manual `docker compose up`), the file simply stays container-owned.
_secrets_path="${GOOGLEFINDMY_SECRETS_PATH:-}"
_data_dir="$(dirname "${_secrets_path:-/data/secrets.json}")"
if [ -n "${GFMY_HOST_UID:-}" ] && [ -d "${_data_dir}" ]; then
  sudo chown -R "${GFMY_HOST_UID}:${GFMY_HOST_GID:-${GFMY_HOST_UID}}" "${_data_dir}" 2>/dev/null || true
  sudo chmod 0700 "${_data_dir}" 2>/dev/null || true
  if [ -f "${_secrets_path}" ]; then
    sudo chmod 0600 "${_secrets_path}" 2>/dev/null || true
  fi
fi

shutdown
