@echo off
rem One-command launcher for the GoogleFindMy-HA login container (Windows).
rem
rem Run this on the Docker host (Docker Desktop), not inside the Home Assistant
rem container. After HACS installs the integration these files already live at
rem config\custom_components\googlefindmy\docker-login\ so no git clone is needed.
rem
rem It cd's to its own folder, creates ./data (where secrets.json is persisted),
rem then builds + runs exactly ONE ephemeral container in the foreground
rem (`docker compose run --rm`), which attaches your terminal so the interactive
rem "Press Enter" login prompt works. Docker Desktop maps the bind-mount
rem permissions, so no UID handoff or chmod is needed here.
rem
rem noVNC is bound to 127.0.0.1 by default (fixed password "secret"). To reach it
rem from another machine, tunnel it, or only on a trusted LAN set
rem GFMY_NOVNC_BIND=0.0.0.0 before running this.
rem
rem Optional one-click handoff, run these two lines in a terminal:
rem   set GFMY_ONECLICK=1
rem   login.cmd
rem only then does this script add docker-compose.oneclick.yml, which publishes the
rem token endpoint on host loopback (127.0.0.1, port 7901). Without it no 7901 port
rem is published at all, so the file handoff and GFMY_CLEARTEXT=1 still start on a
rem host where port 7901 is already in use.
setlocal
pushd "%~dp0"

if not exist data mkdir data

echo [login] Starting the GoogleFindMy-HA login container...
echo [login] When it is up, open http://localhost:7900 (password: secret) and
echo [login] log into Google in the browser view.

rem Compose files: the base file publishes only noVNC (7900). The one-click token
rem endpoint (7901) lives in an OPT-IN overlay, so a host that already uses 7901
rem can never block the file handoff (.\data) or the GFMY_CLEARTEXT=1 output,
rem which do not need that port. Gate matches entrypoint.sh exactly: "1" means on.
set "COMPOSE_FILES=-f docker-compose.yml"
if "%GFMY_ONECLICK%"=="1" set "COMPOSE_FILES=%COMPOSE_FILES% -f docker-compose.oneclick.yml"
if "%GFMY_ONECLICK%"=="1" echo [login] One-click enabled: token endpoint published on 127.0.0.1 port 7901.
rem Passing an explicit -f turns off Compose's implicit override auto-load, so
rem re-add a user override file when there is one (README: shared-network route).
rem At most one, mirroring Compose's own auto-load: it picks a single override
rem file, so merging both spellings here would silently apply a stale leftover.
rem Kept as flat, line-wise statements on purpose: inside a parenthesised block
rem %COMPOSE_FILES% would expand once at parse time, which needs delayed
rem expansion. The goto skips the second spelling after the first hit.
if exist docker-compose.override.yml set "COMPOSE_FILES=%COMPOSE_FILES% -f docker-compose.override.yml"
if exist docker-compose.override.yml goto :override_done
if exist docker-compose.override.yaml set "COMPOSE_FILES=%COMPOSE_FILES% -f docker-compose.override.yaml"
:override_done

rem `run --rm` (not `up`): fresh one-shot container with your terminal attached so
rem the "Press Enter" prompt works; `--service-ports` publishes the ports declared
rem by the selected compose files.
docker compose %COMPOSE_FILES% run --build --service-ports --rm googlefindmy-login

popd
endlocal
