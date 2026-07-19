@echo off
rem One-command launcher for the GoogleFindMy-HA login container (Windows).
rem
rem Run this on the Docker host (Docker Desktop), not inside the Home Assistant
rem container. After HACS installs the integration these files already live at
rem config\custom_components\googlefindmy\docker-login\ so no git clone is needed.
rem
rem It cd's to its own folder, makes sure data\secrets.json exists as a FILE (a
rem bind mount to a missing path would become a directory and break the login),
rem then builds + starts exactly ONE container in the foreground.
setlocal
pushd "%~dp0"

if not exist data mkdir data
if not exist data\secrets.json echo {}>data\secrets.json

echo [login] Starting the GoogleFindMy-HA login container...
echo [login] When it is up, open http://localhost:7900 (password: secret) and
echo [login] log into Google in the browser view.
docker compose up --build --force-recreate --remove-orphans

popd
endlocal
