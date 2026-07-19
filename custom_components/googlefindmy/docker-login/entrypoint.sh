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
# Run the integration's own CLI from the read-only FLAT code mount (/app/gfmy).
# Running the script by path (not `-m custom_components.googlefindmy.main`) keeps
# main.py in its standalone layout: it stubs homeassistant.* and never imports
# the package __init__/config_flow, which require voluptuous + a real Home
# Assistant install (neither is present in this image on purpose). secrets.json
# goes to the writable /data volume via GOOGLEFINDMY_SECRETS_PATH.
cd /app/gfmy
# shellcheck disable=SC2086 -- GFMY_ARGS is an intentional word-split flag list.
python3 main.py ${GFMY_ARGS:-}

shutdown
