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
rem Three ADDRESS ROLES, deliberately separate (mirrors login.sh):
rem   token endpoint 7901  NOT configurable here: pinned to 127.0.0.1 in
rem                        docker-compose.oneclick.yml. That pin is the no-LAN
rem                        guarantee for an endpoint serving Google credentials
rem                        in cleartext, which therefore gets no environment
rem                        override surface at all: one stray variable must not
rem                        be able to widen it.
rem   GFMY_NOVNC_BIND      host bind for the noVNC viewer (7900). Default 127.0.0.1.
rem   GFMY_NOVNC_URL_HOST  address PRINTED for you to open in a browser.
rem                        Defaults to the noVNC bind.
rem
rem The split exists because the two ports serve different consumers: 7901 is
rem machine-to-machine (Home Assistant on this host), 7900 is opened by a browser
rem that usually runs on a DIFFERENT machine than the Docker host.
rem
rem noVNC uses the fixed password "secret". To reach it from another machine,
rem tunnel it, or on a trusted LAN bind a CONCRETE address (preferred over the
rem 0.0.0.0 wildcard):
rem   login.cmd --ip 192.168.1.21
rem Unlike login.sh this script does NOT auto-detect LAN addresses: parsing
rem `ipconfig` output in batch is locale-dependent and unreliable, so pass --ip.
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

set "NOVNC_BIND=%GFMY_NOVNC_BIND%"
if "%NOVNC_BIND%"=="" set "NOVNC_BIND=127.0.0.1"
set "NOVNC_URL_HOST=%GFMY_NOVNC_URL_HOST%"

rem Flat, line-wise argument parsing: inside a parenthesised block the variables
rem would expand once at parse time, which would need delayed expansion.
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--help" goto :usage
if /i "%~1"=="-h" goto :usage
if /i "%~1"=="--ip" goto :parse_ip
set "ARG=%~1"
if /i "%ARG:~0,5%"=="--ip=" goto :parse_ip_inline
echo [login] Unknown option: "%~1" 1>&2
goto :usage_error
:parse_ip
if "%~2"=="" goto :ip_missing
set "NOVNC_BIND=%~2"
set "NOVNC_URL_HOST=%~2"
shift
shift
goto :parse_args
:parse_ip_inline
set "NOVNC_BIND=%ARG:~5%"
if "%NOVNC_BIND%"=="" goto :ip_missing
set "NOVNC_URL_HOST=%NOVNC_BIND%"
shift
goto :parse_args
:args_done

rem A wildcard is a bind pattern, never a browsable address, so never print it
rem as a URL. Unlike login.sh this script does not auto-detect, so it falls back
rem to loopback and tells the user to pass --ip.
if "%NOVNC_URL_HOST%"=="" set "NOVNC_URL_HOST=%NOVNC_BIND%"
if "%NOVNC_URL_HOST%"=="0.0.0.0" set "NOVNC_URL_HOST=127.0.0.1"
if "%NOVNC_URL_HOST%"=="::" set "NOVNC_URL_HOST=127.0.0.1"

rem Classify the BIND, never the printed address: with a wildcard bind the
rem printed address is a concrete one, so branching on it would claim "only this
rem host" for a viewer that is in fact listening on every interface.
set "IS_LOOPBACK="
if "%NOVNC_BIND:~0,4%"=="127." set "IS_LOOPBACK=1"
if /i "%NOVNC_BIND%"=="localhost" set "IS_LOOPBACK=1"
if "%NOVNC_BIND%"=="::1" set "IS_LOOPBACK=1"

set "GFMY_NOVNC_BIND=%NOVNC_BIND%"

if not exist data mkdir data

echo [login] Starting the GoogleFindMy-HA login container...
echo [login] When it is up, open http://%NOVNC_URL_HOST%:7900 (password: secret)
echo [login] and log into Google in the browser view.

rem Tell the truth about who can actually reach that address.
if defined IS_LOOPBACK goto :loopback_hint
echo [login] WARNING: noVNC is bound to %NOVNC_BIND% and is therefore reachable
echo [login] beyond this host, protected only by the fixed password "secret".
echo [login] Use it on a trusted LAN only, and only while logging in.
if "%NOVNC_BIND%"=="0.0.0.0" echo [login] That bind is a wildcard, so the URL above names just one interface it listens on. Prefer login.cmd --ip ^<ADDRESS^>.
goto :hint_done
:loopback_hint
echo [login] noVNC is bound to %NOVNC_BIND%, so only this Docker host reaches it.
echo [login] From another machine either tunnel it:
echo [login]   ssh -L 7900:127.0.0.1:7900 ^<docker-host^>
echo [login] or re-run bound to a LAN address: login.cmd --ip ^<ADDRESS^>
:hint_done

rem Compose files: the base file publishes only noVNC (7900). The one-click token
rem endpoint (7901) lives in an OPT-IN overlay, so a host that already uses 7901
rem can never block the file handoff (.\data) or the GFMY_CLEARTEXT=1 output,
rem which do not need that port. Gate matches entrypoint.sh exactly: "1" means on.
set "COMPOSE_FILES=-f docker-compose.yml"
if "%GFMY_ONECLICK%"=="1" set "COMPOSE_FILES=%COMPOSE_FILES% -f docker-compose.oneclick.yml"
if "%GFMY_ONECLICK%"=="1" echo [login] One-click enabled: token endpoint published on 127.0.0.1 port 7901.
if "%GFMY_ONECLICK%"=="1" echo [login] Home Assistant must reach that address; it does when HA shares this host's network namespace.
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
goto :eof

:usage
call :print_usage
popd
endlocal
exit /b 0

:usage_error
call :print_usage 1>&2
popd
endlocal
exit /b 2

:ip_missing
echo [login] --ip needs an address, e.g. login.cmd --ip 192.168.1.21 1>&2
popd
endlocal
exit /b 2

:print_usage
echo Usage: login.cmd [--ip ^<ADDRESS^>] [--help]
echo.
echo   --ip ^<ADDRESS^>  Bind the noVNC viewer (port 7900) to ^<ADDRESS^> and print
echo                   that address as the URL to open. Use a concrete LAN
echo                   address of this Docker host, e.g. login.cmd --ip 192.168.1.21
echo   --help          Show this help and exit.
echo.
echo Environment (see the comment block at the top of this file):
echo   (the token endpoint 7901 is pinned to 127.0.0.1 in the compose overlay)
echo   GFMY_NOVNC_BIND       host bind for noVNC 7900          (default 127.0.0.1)
echo   GFMY_NOVNC_URL_HOST   address printed for your browser  (default: the bind)
echo   GFMY_ONECLICK=1       add the opt-in overlay that publishes port 7901
goto :eof
