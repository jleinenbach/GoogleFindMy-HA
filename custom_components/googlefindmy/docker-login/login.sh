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
#   2. Make sure ./data/secrets.json exists as a FILE — a bind mount to a
#      non-existent path would otherwise become a directory and break the login.
#   3. Build + start the container in the foreground with a single command,
#      recreating in place so you always end up with exactly ONE container.
set -euo pipefail

cd "$(dirname "$0")"

mkdir -p data
[ -f data/secrets.json ] || printf '{}' > data/secrets.json

echo "[login] Starting the GoogleFindMy-HA login container..."
echo "[login] When it is up, open http://localhost:7900 (password: secret) and"
echo "[login] log into Google in the browser view."
docker compose up --build --force-recreate --remove-orphans
