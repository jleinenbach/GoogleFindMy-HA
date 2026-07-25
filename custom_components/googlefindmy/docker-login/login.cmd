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

rem Strip trailing blanks from EVERY inbound GFMY_* switch before anything reads
rem or forwards one. `set VAR=1 && login.cmd` is the form users reach for, and
rem cmd.exe stores everything up to the `&&` INCLUDING the blank in front of it,
rem so the value arrives as "1 " (measured with a real cmd.exe). Both consumers
rem compare strictly against "1": this script for GFMY_ONECLICK, and
rem entrypoint.sh inside the container for GFMY_ONECLICK and GFMY_CLEARTEXT,
rem which reach it because Compose interpolates our process environment. So the
rem list below is not "what this script reads", it is "what a user can set and
rem what survives into Compose or the container": the ${GFMY_*} names of
rem docker-compose.yml plus GFMY_NOVNC_URL_HOST, which only this script uses.
rem Without the trim the wrong mode starts silently, with exit code 0.
for %%V in (GFMY_ONECLICK GFMY_CLEARTEXT GFMY_ARGS GFMY_HOST_UID GFMY_HOST_GID GFMY_NOVNC_BIND GFMY_NOVNC_URL_HOST GFMY_NOVNC_HARDEN GFMY_NOVNC_TLS) do call :trim_trailing_blanks %%V

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
call :validate_ip "%~2" || goto :ip_invalid
call :set_novnc_from_ip "%~2"
shift
shift
goto :parse_args
:parse_ip_inline
set "CANDIDATE=%ARG:~5%"
if "%CANDIDATE%"=="" goto :ip_missing
call :validate_ip "%CANDIDATE%" || goto :ip_invalid
call :set_novnc_from_ip "%CANDIDATE%"
shift
goto :parse_args
:args_done

rem A wildcard is a bind pattern, never a browsable address, so never print it
rem as a URL. Unlike login.sh this script does not auto-detect, so it falls back
rem to loopback and tells the user to pass --ip.
if "%NOVNC_URL_HOST%"=="" set "NOVNC_URL_HOST=%NOVNC_BIND%"
if "%NOVNC_URL_HOST%"=="0.0.0.0" set "NOVNC_URL_HOST=127.0.0.1"
if "%NOVNC_URL_HOST%"=="::" set "NOVNC_URL_HOST=127.0.0.1"
if "%NOVNC_URL_HOST%"=="[::]" set "NOVNC_URL_HOST=127.0.0.1"

rem Classify the BIND, never the printed address: with a wildcard bind the
rem printed address is a concrete one, so branching on it would claim "only this
rem host" for a viewer that is in fact listening on every interface.
set "IS_LOOPBACK="
if "%NOVNC_BIND:~0,4%"=="127." set "IS_LOOPBACK=1"
if /i "%NOVNC_BIND%"=="localhost" set "IS_LOOPBACK=1"
if "%NOVNC_BIND%"=="::1" set "IS_LOOPBACK=1"
if "%NOVNC_BIND%"=="[::1]" set "IS_LOOPBACK=1"

rem Bracket AFTER classifying, and here rather than in the --ip handler, so the
rem GFMY_NOVNC_BIND / GFMY_NOVNC_URL_HOST environment inputs run through the
rem same normalisation. login.sh normalises at the same point, for that reason.
call :bracket_ipv6 "%NOVNC_BIND%"
set "NOVNC_BIND=%BRACKETED%"
call :bracket_ipv6 "%NOVNC_URL_HOST%"
set "NOVNC_URL_HOST=%BRACKETED%"

set "GFMY_NOVNC_BIND=%NOVNC_BIND%"
rem Re-export the normalised, bracketed browsable host too, so docker-compose can
rem build the noVNC URL (GOOGLEFINDMY_NOVNC_URL). Without this a `login.cmd --ip
rem <ADDRESS>` run would leave GFMY_NOVNC_URL_HOST at its original (usually empty)
rem value and the URL printed inside the container would fall back to localhost.
rem login.sh exports both at the same point, for that reason.
set "GFMY_NOVNC_URL_HOST=%NOVNC_URL_HOST%"

rem LAN-bind hardening (AP-7 + AP-8), mirroring login.sh. On a non-loopback bind
rem raise a single verdict, GFMY_NOVNC_HARDEN=1, that the CONTAINER acts on:
rem entrypoint.sh mints a per-run dense random password (SE_VNC_PASSWORD, fed to
rem x11vnc) and, with GFMY_NOVNC_TLS=1, a self-signed TLS cert. Windows therefore
rem gets the SAME hardening as login.sh WITHOUT any batch-side crypto: the password
rem is generated in the container. Both vars reach it because docker-compose.yml
rem interpolates ${GFMY_*} from this process environment. Classify on the BIND, not
rem the printed address, exactly like the reachability hint below. Unlike login.sh
rem there is no --no-tls opt-out here, so a LAN bind always encrypts the viewer.
set "NOVNC_SCHEME=http"
rem Clear both verdicts first, so a value inherited from the caller's environment
rem cannot harden a loopback run (which must stay the base default). Re-raised
rem below only for a real LAN bind.
set "GFMY_NOVNC_HARDEN="
set "GFMY_NOVNC_TLS="
if not defined IS_LOOPBACK set "GFMY_NOVNC_HARDEN=1"
if not defined IS_LOOPBACK set "GFMY_NOVNC_TLS=1"
if not defined IS_LOOPBACK set "NOVNC_SCHEME=https"

if not exist data mkdir data

echo [login] Starting the GoogleFindMy-HA login container...
rem Loopback keeps the base image's known fixed password, so print it. On a LAN
rem bind the per-run password is minted INSIDE the container, so the host cannot
rem print it: point the user at the container output (both the noVNC-available
rem line and the [AuthFlow] banner show it as "(password: ...)").
if defined IS_LOOPBACK echo [login] When it is up, open %NOVNC_SCHEME%://%NOVNC_URL_HOST%:7900 (password: secret)
if not defined IS_LOOPBACK echo [login] When it is up, open %NOVNC_SCHEME%://%NOVNC_URL_HOST%:7900
if not defined IS_LOOPBACK echo [login] The per-run noVNC password is printed by the container below,
if not defined IS_LOOPBACK echo [login] shown as "(password: ...)" in the noVNC-available line and the [AuthFlow] banner.
echo [login] and log into Google in the browser view.

rem Tell the truth about who can actually reach that address.
if defined IS_LOOPBACK goto :loopback_hint
echo [login] WARNING: noVNC is bound to %NOVNC_BIND% and is therefore reachable
echo [login] beyond this host. For this LAN bind the container replaces the base
echo [login] image's public password with a per-run password, printed in the
echo [login] container output below, and encrypts the viewer with a self-signed
echo [login] certificate: open the https URL above and accept the one-time browser
echo [login] warning. Use it on a trusted LAN only, and only while logging in.
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

:ip_invalid
echo [login] --ip needs an IP address of this Docker host, not a host name. 1>&2
echo [login] The value becomes a docker port bind. Example: --ip 192.168.1.21 1>&2
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

:validate_ip
rem Accept only an IP literal, mirroring is_ip_literal() in login.sh. A host
rem name, or a mistyped second option, would otherwise be printed as a working
rem URL and then handed to docker as a port bind, failing much later with an
rem opaque publish error.
set "CAND=%~1"
if not defined CAND exit /b 1
if "%CAND:~0,1%"=="-" exit /b 1
rem Character allowlist. It rejects host names outright AND makes the value safe
rem to expand in the pipeline below, since no &, | or > can survive it.
set "REST=%CAND%"
for %%x in (0 1 2 3 4 5 6 7 8 9 a b c d e f A B C D E F . : [ ]) do call set "REST=%%REST:%%x=%%"
if defined REST exit /b 1
rem After the allowlist a colon can only come from an IPv6 literal.
set "_NOCOLON=%CAND::=%"
if not "%_NOCOLON%"=="%CAND%" goto :validate_ipv6
rem IPv4 branch: the first allowlist still permits a-f (IPv6 needs them), so
rem narrow it to digits and dots here. Without this, `1.2.3.0a` slips through:
rem `if 0a GTR 255` falls back to a STRING compare, and "0a" sorts below "255".
set "REST=%CAND%"
for %%x in (0 1 2 3 4 5 6 7 8 9 .) do call set "REST=%%REST:%%x=%%"
if defined REST exit /b 1
rem Structure BEFORE splitting. `for /f` collapses a run of delimiters into one
rem and discards leading/trailing ones, so ".1.2.3.4", "1.2.3.4." and "1..2.3.4"
rem would ALL tokenise into the same four clean octets and pass every test in
rem the block below. An empty octet is therefore only visible on the raw string
rem and has to be rejected here, mirroring the `*..*` / `.*` / `*.` guards in
rem is_ip_literal() in login.sh.
set "_T=%CAND:..=%"
if not "%_T%"=="%CAND%" exit /b 1
rem Substring note: "%CAND:~-1%" on a one-character value yields that whole
rem character, which is exactly what is wanted here. CAND="." is caught by the
rem leading test on the line above it, and any other one-character CAND is a
rem digit (the allowlist above left only digits and dots), so it fails both.
if "%CAND:~0,1%"=="." exit /b 1
if "%CAND:~-1%"=="." exit /b 1
rem Exactly four octets, none empty, none above 255. `for /f` skips a line that
rem is nothing but delimiters, so "." or ".." would never enter the block at
rem all: the OCTETS_OK flag turns that silence into a rejection.
set "OCTETS_OK="
for /f "tokens=1-5 delims=." %%a in ("%CAND%") do (
  if not "%%e"=="" exit /b 1
  if "%%d"=="" exit /b 1
  if %%a GTR 255 exit /b 1
  if %%b GTR 255 exit /b 1
  if %%c GTR 255 exit /b 1
  if %%d GTR 255 exit /b 1
  set "OCTETS_OK=1"
)
if not defined OCTETS_OK exit /b 1
exit /b 0

:validate_ipv6
rem Mirrors the IPv6 half of is_ip_literal() in login.sh. The character
rem allowlist alone would accept a lone ":" (and `[::1`, since :bracket_ipv6
rem leaves a leading bracket untouched), and both would reach docker as a port
rem bind after being printed as a working URL.
rem Narrow the shared allowlist first: it permits "." because the IPv4 branch
rem needs it, and only that branch narrows it away again (see the digits-only
rem pass above). The IPv6 half of is_ip_literal() forbids "." outright, so
rem without this line "::1.", "1.2::3" and "fe80::1." would pass here while
rem login.sh rejects them, and this file would stop being the mirror it claims
rem to be in the comment above.
set "_T=%CAND:.=%"
if not "%_T%"=="%CAND%" exit /b 1
set "INNER=%CAND%"
if not "%INNER:~0,1%"=="[" goto :v6_unbracketed
if not "%INNER:~-1%"=="]" exit /b 1
set "INNER=%INNER:~1,-1%"
:v6_unbracketed
if not defined INNER exit /b 1
rem No residual brackets: exactly one enclosing pair, or none at all.
set "_T=%INNER:[=%"
if not "%_T%"=="%INNER%" exit /b 1
set "_T=%INNER:]=%"
if not "%_T%"=="%INNER%" exit /b 1
rem Forbid a ":::" run.
set "_T=%INNER::::=%"
if not "%_T%"=="%INNER%" exit /b 1
rem Structure BEFORE splitting, same class of defect as in the IPv4 branch:
rem `for /f` discards leading and trailing delimiters, so ":1:2:3:4:5:6:7:8" and
rem "1:2:3:4:5:6:7:8:" would both yield eight clean groups and satisfy the count
rem test further down. A leading or trailing ":" is legal only as part of a "::"
rem run, i.e. "::1", "1::" and "::" pass, ":1:..." and "...:8:" do not.
rem Substring note: "%INNER:~0,2%" on a one-character value yields that single
rem character, and "%INNER:~-2%" clamps the same way, so INNER=":" compares as
rem ":" neq "::" and is rejected by the leading test -- the intended verdict.
if "%INNER:~0,1%"==":" if not "%INNER:~0,2%"=="::" exit /b 1
if "%INNER:~-1%"==":" if not "%INNER:~-2%"=="::" exit /b 1
rem Require a "::" run, or at least two separators. The next two lines detect
rem the run and jump away when there is one; the separator probe below them is
rem therefore reached only WITHOUT a run. There the structural guard above has
rem already removed any leading/trailing ":", so every field between the
rem separators is non-empty and a third token exists exactly when a second
rem separator does. Without that guard the token count would be an unreliable
rem stand-in for the separator count, which is the very defect fixed above.
set "_T=%INNER:::=%"
if not "%_T%"=="%INNER%" goto :v6_has_digits
set "_TWO_SEP="
for /f "tokens=1-3 delims=:" %%a in ("%INNER%") do if not "%%c"=="" set "_TWO_SEP=1"
if not defined _TWO_SEP exit /b 1
:v6_has_digits
rem Something addressable must be present; "::" itself is the exception.
if "%INNER%"=="::" exit /b 0
set "_T=%INNER%"
for %%x in (0 1 2 3 4 5 6 7 8 9 a b c d e f A B C D E F) do call set "_T=%%_T:%%x=%%"
if "%_T%"=="%INNER%" exit /b 1
rem At most ONE "::" run: two are ambiguous, so RFC 4291 forbids it.
rem `%VAR:*::=%` drops everything up to and including the first run.
set "_HAS_RUN="
set "_T=%INNER:::=%"
if not "%_T%"=="%INNER%" set "_HAS_RUN=1"
if defined _HAS_RUN (
  set "_TAIL=%INNER:*::=%"
) else (
  set "_TAIL="
)
if defined _TAIL call :v6_no_second_run "%_TAIL%" || exit /b 1
rem Group width (max four hex digits) and count. `for /f` collapses repeated
rem delimiters, so a compressed address simply yields fewer tokens. That is safe
rem to lean on ONLY because the structural guard above already rejected an empty
rem group at either end; the token count may stand in for the group count, never
rem for the shape of the raw value.
for /f "tokens=1-9 delims=:" %%a in ("%INNER%") do (
  if not "%%i"=="" exit /b 1
  call :v6_group "%%a" || exit /b 1
  call :v6_group "%%b" || exit /b 1
  call :v6_group "%%c" || exit /b 1
  call :v6_group "%%d" || exit /b 1
  call :v6_group "%%e" || exit /b 1
  call :v6_group "%%f" || exit /b 1
  call :v6_group "%%g" || exit /b 1
  call :v6_group "%%h" || exit /b 1
  if not defined _HAS_RUN if "%%h"=="" exit /b 1
  rem With a compression run at most SEVEN groups may be written out,
  rem since "::" stands for one or more omitted ones: "1:2:3:4:5:6:7:8::"
  rem has eight explicit groups and is therefore already too long.
  if defined _HAS_RUN if not "%%h"=="" exit /b 1
)
exit /b 0

:v6_no_second_run
rem Reject a second "::" run in the tail after the first one.
set "_TT=%~1"
set "_T2=%_TT:::=%"
if not "%_T2%"=="%_TT%" exit /b 1
exit /b 0

:v6_group
rem An IPv6 group is one to four hex digits. Empty means the token was produced
rem by the compressed run and carries no width of its own.
set "_G=%~1"
if not defined _G exit /b 0
if not "%_G:~4%"=="" exit /b 1
exit /b 0

:set_novnc_from_ip
rem --ip sets BOTH roles, but stores the value RAW. Bracketing happens once,
rem after the wildcard/loopback classification below, so that classification
rem compares against the same spelling the operator typed and so the
rem GFMY_NOVNC_BIND environment path gets normalised too.
set "NOVNC_BIND=%~1"
set "NOVNC_URL_HOST=%~1"
goto :eof

:bracket_ipv6
rem An IPv6 literal needs brackets in the printed URL (so the port can be told
rem from the address) and in docker's port syntax. Returns via BRACKETED.
rem The colon test runs on a quoted substitution rather than `echo | find`,
rem because this routine also sees the UNVALIDATED environment values.
set "BRACKETED=%~1"
set "_NOCOLON=%BRACKETED::=%"
if "%_NOCOLON%"=="%BRACKETED%" goto :eof
if "%BRACKETED:~0,1%"=="[" goto :eof
set "BRACKETED=[%BRACKETED%]"
goto :eof

:trim_trailing_blanks
rem Removes trailing blanks from the variable NAMED in %1, in place. Recurses
rem through `goto :` (which keeps %1 bound) instead of a delayed-expansion loop,
rem so the file needs no `setlocal EnableDelayedExpansion` and the routine is
rem safe to call before the environment has been normalised. `call set` performs
rem the second expansion round that turns the name into its value.
if not defined %~1 exit /b 0
call set "_TTB=%%%~1%%"
if not "%_TTB:~-1%"==" " exit /b 0
call set "%~1=%%%~1:~0,-1%%"
goto :trim_trailing_blanks
