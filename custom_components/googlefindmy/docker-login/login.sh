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
#   2. Create ./data (where the produced secrets.json is persisted) and make it
#      writable by the container user. The image runs as "seluser", whose UID may
#      differ from yours; without this the atomic write of secrets.json would
#      fail with a permission error.
#   3. Build + start the container in the foreground with a single command,
#      recreating in place so you always end up with exactly ONE container.
#
# noVNC is bound to 127.0.0.1 by default (the viewer uses the base image's fixed
# password "secret"). To reach it from another machine, tunnel with
#   ssh -L 7900:127.0.0.1:7900 <docker-host>
# or, only on a trusted LAN, export GFMY_NOVNC_BIND=0.0.0.0 before running this.
set -euo pipefail

cd "$(dirname "$0")"

mkdir -p data
# Make the token volume writable by the container user (UID may differ).
chmod 0777 data 2>/dev/null || true

echo "[login] Starting the GoogleFindMy-HA login container..."
echo "[login] When it is up, open http://localhost:7900 (password: secret) and"
echo "[login] log into Google in the browser view."
docker compose up --build --force-recreate --remove-orphans
