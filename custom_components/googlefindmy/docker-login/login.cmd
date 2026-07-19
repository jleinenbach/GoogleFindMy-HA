@echo off
rem One-command launcher for the GoogleFindMy-HA login container (Windows).
rem
rem Run this on the Docker host (Docker Desktop), not inside the Home Assistant
rem container. After HACS installs the integration these files already live at
rem config\custom_components\googlefindmy\docker-login\ so no git clone is needed.
rem
rem It cd's to its own folder, creates ./data (where secrets.json is persisted),
rem then builds + starts exactly ONE container in the foreground. Docker Desktop
rem handles the bind-mount permissions, so no chmod is needed here.
rem
rem noVNC is bound to 127.0.0.1 by default (fixed password "secret"). To reach it
rem from another machine, tunnel it, or only on a trusted LAN set
rem GFMY_NOVNC_BIND=0.0.0.0 before running this.
setlocal
pushd "%~dp0"

if not exist data mkdir data

echo [login] Starting the GoogleFindMy-HA login container...
echo [login] When it is up, open http://localhost:7900 (password: secret) and
echo [login] log into Google in the browser view.
docker compose up --build --force-recreate --remove-orphans

popd
endlocal
