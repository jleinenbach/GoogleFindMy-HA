#!/usr/bin/env bash
#
# One-command launcher for the GoogleFindMy-HA login container (Linux/macOS/QNAP).
#
# Run this on the Docker *host* (where the Docker daemon runs), not inside the
# Home Assistant container — HA has no Docker socket. After HACS installs the
# integration these files already live at
#   config/custom_components/googlefindmy/docker-login/
# so no `git clone` is needed; just run this script there.
#
# What it does:
#   1. cd to this script's own directory (you don't have to navigate there).
#   2. Create ./data (where the produced secrets.json is persisted). The container
#      takes ownership of it for the run (via passwordless sudo in the selenium
#      base image), so no world-writable host chmod is needed.
#   3. Export your host UID/GID so the container hands the finished secrets.json
#      back to you as owner with owner-only 0600 (you can read/copy it; no other
#      local account can).
#   4. Build + run exactly ONE ephemeral container in the foreground
#      (`docker compose run --rm`), which also attaches your terminal so the
#      interactive "Press Enter" login prompt reaches Python.
#
# noVNC is bound to 127.0.0.1 by default (the viewer uses the base image's fixed
# password "secret"). To reach it from another machine, tunnel with
#   ssh -L 7900:127.0.0.1:7900 <docker-host>
# or, only on a trusted LAN, export GFMY_NOVNC_BIND=0.0.0.0 before running this.
#
# Optional one-click handoff:
#   GFMY_ONECLICK=1 ./login.sh
# only then does this script add docker-compose.oneclick.yml, which publishes the
# token endpoint on host loopback (127.0.0.1, port 7901). Without it no 7901 port
# is published at all, so the file handoff and GFMY_CLEARTEXT=1 still start on a
# host where port 7901 is already in use.
set -euo pipefail

cd "$(dirname "$0")"

mkdir -p data

# Hand the finished secrets.json back to the invoking host user as owner (0600).
# The container chowns ./data to itself for the run, then back to these IDs.
export GFMY_HOST_UID="$(id -u)"
export GFMY_HOST_GID="$(id -g)"

echo "[login] Starting the GoogleFindMy-HA login container..."
echo "[login] When it is up, open http://localhost:7900 (password: secret) and"
echo "[login] log into Google in the browser view."

# Compose files: the base file publishes only noVNC (7900). The one-click token
# endpoint (7901) lives in an OPT-IN overlay, so a host that already uses 7901
# can never block the file handoff (./data) or the GFMY_CLEARTEXT=1 output,
# which do not need that port. Gate matches entrypoint.sh exactly: "1" means on.
compose_files=(-f docker-compose.yml)
if [ "${GFMY_ONECLICK:-}" = "1" ]; then
  compose_files+=(-f docker-compose.oneclick.yml)
  echo "[login] One-click enabled: token endpoint published on 127.0.0.1 port 7901."
fi
# Passing an explicit -f turns off Compose's implicit override auto-load, so
# re-add a user override file when there is one (README: shared-network route).
# At most one, mirroring Compose's own auto-load: it picks a single override
# file, so merging both spellings here would silently apply a stale leftover.
for _override in docker-compose.override.yml docker-compose.override.yaml; do
  if [ -f "${_override}" ]; then
    compose_files+=(-f "${_override}")
    break
  fi
done

# `run --rm` (not `up`): fresh one-shot container, removed on exit, with your
# terminal attached so the interactive "Press Enter" login prompt works.
# `--service-ports` publishes the ports declared by the selected compose files.
docker compose "${compose_files[@]}" run --build --service-ports --rm googlefindmy-login
