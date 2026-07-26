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

# --- Direct-Compose LAN-hardening fallback (AP-7/AP-8 verdict derivation) -----
# The launchers (login.sh / login.cmd) raise GFMY_NOVNC_HARDEN/_TLS for a LAN bind
# and pass them across the host->container boundary. The SUPPORTED direct path
#   docker compose run --build --service-ports --rm googlefindmy-login
# runs NO launcher: it binds ${GFMY_NOVNC_BIND}:7900 on the host (see `ports:` in
# docker-compose.yml) but sets no verdict. Without this block a LAN bind on that
# path would publish port 7900 with the base image's fixed "secret" over plain
# http -- the exact fixed-credential hole the launchers close. So the CONTAINER
# derives the same verdict the launchers do, keying on the bind (the single source
# of truth for "is this reachable off-host"): a non-loopback bind is a LAN exposure
# and MUST be hardened. This runs only when no explicit launcher verdict is present
# (GFMY_NOVNC_HARDEN != 1), so it never overrides a launcher decision -- including
# an intentional launcher --no-tls run, which already sets HARDEN=1 and thus skips
# this block entirely. Fail-closed: any doubt about a non-loopback bind hardens.
if [ "${GFMY_NOVNC_HARDEN:-}" != "1" ]; then
  _gfmy_bind="${GFMY_NOVNC_BIND:-127.0.0.1}"
  # Strip one enclosing IPv6 bracket pair ("[::1]" -> "::1") so the classifier,
  # which mirrors login.sh's is_loopback_addr/is_wildcard_addr, sees the bare host.
  _gfmy_bind_bare="${_gfmy_bind#[}"
  _gfmy_bind_bare="${_gfmy_bind_bare%]}"
  case "${_gfmy_bind_bare}" in
    127.* | ::1 | localhost | "")
      # Loopback (or unset): nothing off-host can reach it, keep the historical
      # plain-http "secret" default. Also clear an inherited TLS verdict: login.sh
      # clears both up front for the same reason, so a stray GFMY_NOVNC_TLS from the
      # caller's environment cannot switch a loopback run to https-with-fixed-secret.
      # HARDEN is already != 1 here by the outer guard.
      export GFMY_NOVNC_TLS=""
      ;;
    *)
      # Non-loopback (concrete LAN IP or 0.0.0.0/:: wildcard): network-reachable.
      # ALWAYS mint the per-run password -- that is what closes the fixed-secret hole.
      export GFMY_NOVNC_HARDEN=1
      # Determine a certificate SAN. A concrete bind IP is a usable SAN; a wildcard
      # (0.0.0.0/::/*) is not a single browsable host. When we adopt the bind as the
      # SAN we must also rebuild the browse URL to name the SAME host: compose builds
      # GOOGLEFINDMY_NOVNC_URL from URL_HOST ("localhost" when the host did not set
      # it), so without this the banner would print https://localhost while the cert
      # certifies the bind IP -- a broken, mismatched promise.
      case "${_gfmy_bind_bare}" in
        0.0.0.0 | :: | "*")
          : ;;
        *)
          if [ -z "${GFMY_NOVNC_URL_HOST:-}" ]; then
            export GFMY_NOVNC_URL_HOST="${_gfmy_bind}"
            export GOOGLEFINDMY_NOVNC_URL="http://${_gfmy_bind}:7900"
          fi
          ;;
      esac
      # Enable TLS only when a SAN is available (so we never raise an https promise
      # the TLS block cannot keep) AND the run did not opt out (GFMY_NOVNC_TLS=0, the
      # direct-Compose analogue of the launcher --no-tls). A wildcard bind without an
      # explicit URL_HOST therefore stays plain http + per-run password: the hole is
      # closed, the transport is honestly plain (documented posture), no false https.
      if [ -n "${GFMY_NOVNC_URL_HOST:-}" ] && [ "${GFMY_NOVNC_TLS:-}" != "0" ]; then
        export GFMY_NOVNC_TLS=1
      else
        export GFMY_NOVNC_TLS=""
      fi
      ;;
  esac
  unset _gfmy_bind _gfmy_bind_bare
fi

# --- Per-run noVNC password for a LAN-bound viewer (AP-7) ---------------------
# Only when the launcher raised the LAN-hardening verdict (GFMY_NOVNC_HARDEN=1,
# set for a non-loopback/wildcard bind); the loopback/tunnel default never sets
# it, so nothing here runs and the base image keeps its public "secret" password.
# The password MUST be minted and exported HERE, before supervisord starts the VNC
# program, so start-vnc.sh (a supervisor child that reads
# VNC_PASSWORD=${VNC_PASSWORD:-$SE_VNC_PASSWORD}) inherits it in time. This is the
# same timing class as the TLS-cert block below, and both run before the
# `supervisord &` line further down.
#
# This entrypoint is now the crypto OWNER: neither launcher (login.sh on
# Linux/macOS, login.cmd on Windows) does any host-side crypto, so BOTH get
# identical hardening. The launcher only decides LAN-vs-loopback and passes the
# verdict across the host->container boundary via docker-compose.yml.
#
# Classic VNC auth (x11vnc -storepasswd, which the base image feeds SE_VNC_PASSWORD
# into) only uses the FIRST 8 BYTES of the password, so a long passphrase would be
# silently truncated and its nominal length would misrepresent the real strength.
# We therefore mint a DENSE 8-character password so the full entropy lives inside
# those 8 effective bytes.
#
# Charset: letters + the keyboard-easy symbols ". , = - _ + @". The equally obvious
# "! ? % $" are deliberately EXCLUDED, each has a SILENT-failure path in the code
# this value flows through: "%" is the cmd.exe escape char (login.cmd), "!" is
# cmd.exe delayed expansion, "?" glob-expands in the base image's UNQUOTED
# `x11vnc -storepasswd ${VNC_PASSWORD}`, and "$" is a shell metachar. Over 8
# effective bytes they add negligible entropy, so dropping them costs nothing.
# python3 (the venv CPython) is guaranteed in this image; the /dev/urandom
# rejection-sampling fallback covers the (practically impossible) case it fails.
# If BOTH CSPRNG sources are unavailable the mint FAILS CLOSED (aborts the launch)
# rather than emit a weak, guessable credential onto a network-reachable viewer.
if [ "${GFMY_NOVNC_HARDEN:-}" = "1" ]; then
  _gfmy_pw="$(python3 -c 'import secrets, string
alphabet = string.ascii_letters + ".,=-_+@"
print("".join(secrets.choice(alphabet) for _ in range(8)))' 2>/dev/null || true)"
  if [ -z "${_gfmy_pw}" ]; then
    # Fallback CSPRNG: a /dev/urandom byte stream with rejection sampling, so the
    # naive modulo bias of `byte % n` never skews the character distribution. The
    # 256-(256%n) limit rejects the top partial bucket that would otherwise bias it.
    _gfmy_chars='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.,=-_+@'
    _gfmy_n=${#_gfmy_chars}
    _gfmy_pw=""
    _gfmy_limit=$(( 256 - (256 % _gfmy_n) ))
    while [ "${#_gfmy_pw}" -lt 8 ]; do
      _gfmy_byte="$(od -An -N1 -tu1 /dev/urandom 2>/dev/null | tr -dc '0-9')"
      # Fail closed. If /dev/urandom yields no byte here, python3's secrets AND
      # the kernel CSPRNG are both unavailable. Deriving the LAN-exposed VNC
      # password from Bash's non-cryptographic $RANDOM would defeat the hardening
      # precisely on the error path (owasp Fail-Closed): a predictable credential
      # on a network-reachable viewer is worse than not starting. Abort BEFORE
      # supervisord (traps are registered further down), so no viewer is served.
      if [ -z "${_gfmy_byte}" ]; then
        echo "[entrypoint] FATAL: no CSPRNG available (python3 'secrets' and" >&2
        echo "[entrypoint] /dev/urandom both failed); refusing to serve a LAN-bound" >&2
        echo "[entrypoint] noVNC viewer with a predictable password. Aborting." >&2
        exit 1
      fi
      [ "${_gfmy_byte}" -lt "${_gfmy_limit}" ] || continue
      _gfmy_pw="${_gfmy_pw}${_gfmy_chars:$(( _gfmy_byte % _gfmy_n )):1}"
    done
  fi
  # SE_VNC_PASSWORD is inherited by the base image's start-vnc.sh (x11vnc), so the
  # viewer stops accepting the public "secret". GOOGLEFINDMY_NOVNC_PASSWORD carries
  # the SAME value, so the banner below (noVNC-available line) and auth_flow.py's
  # sign-in prompt both show the real per-run password instead of the "secret"
  # default. On a loopback login neither is set, so both keep showing "secret".
  export SE_VNC_PASSWORD="${_gfmy_pw}"
  export GOOGLEFINDMY_NOVNC_PASSWORD="${_gfmy_pw}"
  unset _gfmy_pw _gfmy_chars _gfmy_n _gfmy_limit _gfmy_byte
fi

# --- Self-signed TLS for a LAN-bound noVNC viewer (AP-8) ----------------------
# Only when the launcher opted into TLS for a concrete LAN bind (GFMY_NOVNC_TLS=1);
# the loopback/tunnel default never sets it, so no certificate is created and the
# base image's plain start-novnc.sh runs completely unchanged. The cert MUST be
# minted HERE, before supervisord starts the noVNC program, so start-novnc.sh
# already sees it (a later write would race the proxy start). The IP is only known
# at runtime (chosen interactively), so a cert carrying the chosen IP as a SAN --
# which modern Chrome requires for an IP certificate -- cannot be baked into the
# image. openssl failing (or absent) is non-fatal: we fall back to the plain
# viewer so the login always proceeds.
if [ "${GFMY_NOVNC_TLS:-}" = "1" ]; then
  _tls_dir="${HOME:-/home/seluser}/gfmy-novnc"
  _tls_pem="${_tls_dir}/combined.pem"
  # The bind/URL host may be a bracketed IPv6 literal ("[::1]"); openssl wants the
  # bare address in the SAN, so strip one enclosing bracket pair.
  _san_host="${GFMY_NOVNC_URL_HOST:-}"
  _san_host="${_san_host#[}"
  _san_host="${_san_host%]}"
  # A SAN entry must be TYPED: modern Chrome validates an IP cert against an IP:
  # SAN and a hostname cert against a DNS: SAN, and openssl rejects a literal
  # "IP:<hostname>" outright (nonzero exit). Hardcoding IP: would therefore drop a
  # hostname URL_HOST to the plain fallback while the launcher already promised
  # https -- a broken promise. Classify instead: a colon is an IPv6 literal, any
  # character outside [0-9.] means a hostname, only digits and dots mean IPv4.
  case "${_san_host}" in
    *:*)        _san_alt="IP:${_san_host}" ;;
    *[!0-9.]*)  _san_alt="DNS:${_san_host}" ;;
    *)          _san_alt="IP:${_san_host}" ;;
  esac
  if command -v openssl >/dev/null 2>&1 && [ -n "${_san_host}" ]; then
    mkdir -p "${_tls_dir}"
    # -keyout and -out to SEPARATE files (same path would overwrite one with the
    # other), then concatenate cert+key into combined.pem for websockify.
    if openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "${_tls_dir}/key.pem" -out "${_tls_dir}/cert.pem" \
        -days 1 -subj "/CN=${_san_host}" \
        -addext "subjectAltName=${_san_alt}" >/dev/null 2>&1; then
      cat "${_tls_dir}/cert.pem" "${_tls_dir}/key.pem" > "${_tls_pem}"
      chmod 0600 "${_tls_pem}"
      # websockify reads only combined.pem (start-novnc.sh points both --cert and
      # --key at it), so drop the standalone key.pem/cert.pem: leaving the bare
      # private key on disk (created under the default umask, world-readable)
      # serves no purpose and is one more copy of the key than necessary.
      rm -f "${_tls_dir}/cert.pem" "${_tls_dir}/key.pem"
      export GFMY_NOVNC_CERT="${_tls_pem}"
      # --ssl-only serves https only, so the printed URL must be https too, or the
      # user opens http:// and gets a blank page.
      case "${GOOGLEFINDMY_NOVNC_URL:-}" in
        http://*) GOOGLEFINDMY_NOVNC_URL="https://${GOOGLEFINDMY_NOVNC_URL#http://}" ;;
      esac
      export GOOGLEFINDMY_NOVNC_URL
      echo "[entrypoint] Self-signed TLS enabled for noVNC (SAN ${_san_alt})."
    else
      echo "[entrypoint] WARNING: openssl could not create the noVNC certificate;" >&2
      echo "[entrypoint] continuing WITHOUT TLS (plain http viewer)." >&2
    fi
  else
    echo "[entrypoint] WARNING: TLS was requested but openssl or the target IP is" >&2
    echo "[entrypoint] unavailable; continuing WITHOUT TLS (plain http viewer)." >&2
  fi
fi

"${VENV_PATH}/bin/supervisord" --configuration /etc/supervisord.conf &
SUPERVISOR_PID=$!

# cleanup runs on EVERY exit path: normal completion, a nonzero main.py exit
# (Chrome/login/network failure aborting under `set -e`), or SIGTERM/SIGINT. It
# performs the ownership handoff FIRST, so a produced (even partial) owner-only
# secrets.json is always returned to the host user. Otherwise, after the pre-run
# chown of /data to the container UID below, a failed run would leave the host
# unable to read or delete the file. It then stops supervisor. The guard keeps
# it idempotent so the EXIT trap and a signal-driven exit cannot double-run it.
_cleaned=0
# PID of the child currently being waited on, or "" when none is running. Every
# child this entrypoint WAITS ON goes through this single slot (supervisord is
# backgrounded separately and torn down in cleanup, see
# wait_for_tracked_child below), so on_signal has exactly one place to check. In
# the gaps between children (waiting for the X display, between main.py and the
# token server) the slot is empty on purpose and on_signal exits directly.
CHILD_PID=""
# Set to the conventional 128+signal code once a terminating signal has been
# relayed to a child. Needed because a child may absorb the signal and still exit
# 0 (main.py does exactly that on SIGINT), leaving no trace in the wait status.
# Read by exit_if_terminating below, which every wait is followed by.
_terminating=""
function cleanup {
  [ "${_cleaned}" = 1 ] && return
  _cleaned=1

  # Ownership handoff: main.py writes secrets.json 0600 owned by the container
  # user. Hand the produced credentials back to the *host* user (UID/GID passed
  # by the launcher) while KEEPING owner-only 0600, so you can read/copy the file
  # for the Home Assistant import and no other local account can (unlike a
  # world-readable 0644 relaxation). Without a host UID (e.g. a manual
  # `docker compose up`) the file simply stays container-owned.
  _secrets_path="${GOOGLEFINDMY_SECRETS_PATH:-}"
  _data_dir="$(dirname "${_secrets_path:-/data/secrets.json}")"
  if [ -n "${GFMY_HOST_UID:-}" ] && [ -d "${_data_dir}" ]; then
    sudo chown -R "${GFMY_HOST_UID}:${GFMY_HOST_GID:-${GFMY_HOST_UID}}" "${_data_dir}" 2>/dev/null || true
    sudo chmod 0700 "${_data_dir}" 2>/dev/null || true
    if [ -f "${_secrets_path}" ]; then
      sudo chmod 0600 "${_secrets_path}" 2>/dev/null || true
    fi
  fi

  kill -s SIGTERM "${SUPERVISOR_PID}" 2>/dev/null || true
  wait "${SUPERVISOR_PID}" 2>/dev/null || true
}
# On a terminating signal, FORWARD it to the tracked child instead of exiting
# straight away: every long-running child runs in the background so bash can react
# to the signal at once and relay it. A foreground child would make bash defer these
# traps until it exits on its own, so `docker stop` would escalate to SIGKILL and the
# EXIT cleanup (ownership handoff + supervisor shutdown) would never run. Between two
# tracked children (still waiting for the X display, or between main.py and the token
# server) there is nothing to forward to, so fall back to exiting with the
# conventional 128+signal code. The single EXIT trap runs the handoff+shutdown exactly
# once, on every path.
function on_signal {
  local sig="$1" code="$2"
  if [ -n "${CHILD_PID}" ] && kill -0 "${CHILD_PID}" 2>/dev/null; then
    _terminating="${code}"
    kill -s "${sig}" "${CHILD_PID}" 2>/dev/null || true
  else
    exit "${code}"
  fi
}

# Wait for the child in CHILD_PID and return ITS exit status, without tripping
# `set -e` in the caller (call as `wait_for_tracked_child || rc=$?`). `wait` returns
# >128 when a forwarded signal interrupts the wait before the child is reaped, so
# re-wait until the child has actually exited. Clearing the slot afterwards makes
# on_signal fall back to a direct `exit` in the gaps between children: `kill -0`
# would already fail on a reaped PID, but that PID number can since have been
# recycled by an unrelated process, and signalling THAT is what the reset prevents.
function wait_for_tracked_child {
  local rc=0
  # Defensive, for a future call site: today both callers assign CHILD_PID=$!
  # immediately before, so the slot is never empty here. Were it ever called with
  # an empty slot, `wait ""` would print a usage error and return 1, which the
  # `-eq 0` gates below would read as a failed login.
  [ -n "${CHILD_PID}" ] || return 0
  wait "${CHILD_PID}" || rc=$?
  while [ "${rc}" -gt 128 ] && kill -0 "${CHILD_PID}" 2>/dev/null; do
    rc=0
    wait "${CHILD_PID}" || rc=$?
  done
  CHILD_PID=""
  return "${rc}"
}

# Call after EVERY wait_for_tracked_child, passing that child's status. A tracked
# child dies on the relayed signal and its wait() returns NORMALLY, so the status
# alone cannot tell "finished" from "we are shutting down" -- main.py even catches
# KeyboardInterrupt and returns 0 (main.py: `except KeyboardInterrupt: print`), and
# a server that shuts down gracefully may do the same. Without this the run would
# continue into the post-login handoff after a `docker stop`: starting a token
# server nobody can still reach, or printing the credential bundle into the
# container log, and reporting success either way.
#
# Two independent shutdown paths, because neither status nor marker covers both:
# the marker is set only when WE relayed the signal, and the >128 status appears
# only when the child died FROM it (Ctrl-C on a TTY reaches the whole process
# group, so the child can be gone before we ever relay).
function exit_if_terminating {
  local rc="${1:-0}"
  if [ -n "${_terminating}" ]; then
    exit "${_terminating}"
  fi
  if [ "${rc}" -gt 128 ]; then
    exit "${rc}"
  fi
}

# --- One-click handoff banner (track B) --------------------------------------
# A function, not an inline block, so the regression test can RUN it against
# several bind values instead of grepping the script for hopeful substrings.
#
# The address printed here must be the one the endpoint is actually published on.
# That address is decided on the HOST (`ports:` in docker-compose.oneclick.yml,
# from GFMY_ONECLICK_BIND), so the overlay forwards the variable into the
# container and this function follows it -- never the other way round, exactly
# like the noVNC URL follows GFMY_NOVNC_BIND at the top of this file. Printing a
# fixed 127.0.0.1 next to a widened publish is not a cosmetic mismatch: it is the
# one field the operator types into Home Assistant.
function print_oneclick_banner {
  local bind bare loopback
  bind="${GFMY_ONECLICK_BIND:-127.0.0.1}"
  # An empty value means "nobody set it": Compose interpolates its own
  # `:-127.0.0.1` default in that case, so loopback is what actually gets bound.
  [ -n "${bind}" ] || bind="127.0.0.1"
  # Strip one enclosing IPv6 bracket pair ("[::1]" -> "::1") before classifying,
  # mirroring the noVNC derivation above; the brackets stay in what we print,
  # because that is the form a URL/host field needs.
  bare="${bind#[}"
  bare="${bare%]}"
  case "${bare}" in
    127.* | ::1 | localhost) loopback=1 ;;
    *) loopback=0 ;;
  esac

  echo ""
  echo "=================================================================="
  echo "[entrypoint] ONE-CLICK login ready. In Home Assistant choose the"
  echo "[entrypoint] 'Container login' auth method and enter:"
  echo "[entrypoint]     host: ${bind}   port: 7901"
  echo "[entrypoint]     pairing code: ${GFMY_PAIRING_CODE}"
  echo "[entrypoint]"
  if [ "${loopback}" -eq 1 ]; then
    echo "[entrypoint] That address is the loopback of the DOCKER HOST, not of Home"
    echo "[entrypoint] Assistant. It works when Home Assistant shares this host's"
    echo "[entrypoint] network namespace (HAOS, HA Core, network_mode: host). A Home"
    echo "[entrypoint] Assistant in its own BRIDGED container cannot reach it: there,"
    echo "[entrypoint] 127.0.0.1 is the HA container itself. Three ways out:"
    echo "[entrypoint]   * put both containers on one Docker network (the launcher"
    echo "[entrypoint]     needs --use-aliases for the name to resolve) and enter this"
    echo "[entrypoint]     container's service name here instead;"
    echo "[entrypoint]   * skip the port and let Home Assistant import the file"
    echo "[entrypoint]     docker-login/data/secrets.json, which it does on its own;"
    echo "[entrypoint]   * publish on a LAN address of this host and enter that:"
    echo "[entrypoint]     GFMY_ONECLICK_BIND=<ADDRESS> GFMY_ONECLICK=1 bash login.sh"
    echo "[entrypoint] From another machine you can also tunnel:"
    echo "[entrypoint]   ssh -L 7901:127.0.0.1:7901 <host>"
    echo "[entrypoint] but only if the tunnel's local end sits inside the network"
    echo "[entrypoint] namespace Home Assistant itself uses."
  else
    echo "[entrypoint] That address is reachable beyond this host, and this endpoint"
    echo "[entrypoint] serves your Google credentials UNENCRYPTED (plain http). Use it"
    echo "[entrypoint] inside a trusted LAN or on this host only, and only for the few"
    echo "[entrypoint] seconds the handoff takes -- never across the internet. What"
    echo "[entrypoint] guards it meanwhile: the one-time pairing code above, a"
    echo "[entrypoint] 300-second expiry, a single successful fetch, and a lockout"
    echo "[entrypoint] after five wrong codes."
  fi
  echo "[entrypoint]"
  echo "[entrypoint] If Home Assistant cannot reach the port, the host publish is"
  echo "[entrypoint] missing: 7901 is an OPT-IN overlay so that a busy port never"
  echo "[entrypoint] blocks the file/cleartext tracks. The launcher adds it for you"
  echo "[entrypoint] (GFMY_ONECLICK=1 bash login.sh); a manual run needs it spelled out:"
  echo "[entrypoint]   docker compose -f docker-compose.yml -f docker-compose.oneclick.yml \\"
  echo "[entrypoint]     run --build --service-ports --rm googlefindmy-login"
  echo "=================================================================="
  echo ""
}

trap cleanup EXIT
trap 'on_signal TERM 143' TERM
trap 'on_signal INT 130' INT

echo "[entrypoint] Waiting for X display ${DISPLAY} to come up..."
for i in $(seq 1 30); do
  if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    echo "[entrypoint] Display ready."
    break
  fi
  sleep 1
done

# Neutral status line only: it must NOT read as the call to action. The single
# actionable instruction block (open the URL, sign in to the Chrome window that
# comes up there) is printed by the login flow (auth_flow.py) right before it
# starts Chrome, and ONLY on a real login -- a second run with an existing
# secrets.json returns early and shows no such block. Uses the real URL passed
# via compose (LAN address with --ip, else
# loopback); a manual `docker run` falls back to localhost.
echo "[entrypoint] noVNC available at ${GOOGLEFINDMY_NOVNC_URL:-http://localhost:7900} (password: ${GOOGLEFINDMY_NOVNC_PASSWORD:-secret})."

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
# Mark the standalone login flow as running inside the container so auth_flow.py
# prints container-correct instructions. docker-compose.yml already sets this,
# but default it on here too so a bare `docker run` of this image (no compose
# env) is still recognised as a container login. `:=` never overrides an
# explicit value; `export` makes the backgrounded python child inherit it.
: "${GOOGLEFINDMY_CONTAINER_LOGIN:=1}"
export GOOGLEFINDMY_CONTAINER_LOGIN

# Grid-readiness wait (AP-2). The Selenium Standalone grid (port 4444) boots in
# the background under supervisord and floods this console with a "Started
# Selenium Standalone ... 4444" banner. That banner is irrelevant to the login
# (the flow drives undetected-chromedriver directly, not the grid), but if it
# prints AFTER the auth instructions the user mistakes the trailing 4444 line for
# a hang. Wait for the grid to report ready first so the single actionable
# instruction block from auth_flow.py is the LAST thing on screen. Bounded and
# best-effort: on timeout (or without curl) we simply continue, and this touches
# none of the trap/wait/signal machinery above.
if command -v curl >/dev/null 2>&1; then
  echo "[entrypoint] Waiting for the Selenium grid to finish booting..."
  for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:4444/status >/dev/null 2>&1; then
      echo "[entrypoint] Selenium grid ready."
      break
    fi
    sleep 1
  done
fi

cd /app/gfmy
# Start the CLI in the BACKGROUND and wait on it, so a SIGTERM/SIGINT reaches bash
# immediately and on_signal can relay it to the child (a foreground child defers the
# traps until it exits -> docker escalates to SIGKILL -> no cleanup).
#
# Backgrounding in a non-interactive script (job control off) has two side effects we
# must undo, or the first-run login breaks:
#   1. stdin: bash points an async child's stdin at /dev/null unless an explicit
#      redirection overrides it. main.py's `input("Enter your Google account email:")`
#      prompt would then hit EOF and abort the login. The `<&0` at the async boundary (NOT
#      inside the subshell, where fd 0 is already /dev/null) restores the entrypoint's
#      terminal stdin (which `docker compose run` attaches).
#   2. SIGINT/SIGQUIT: bash hard-ignores (SIG_IGN) these in an async child; Python
#      inherits SIG_IGN and then declines to install its KeyboardInterrupt handler, so
#      a relayed SIGINT (Ctrl-C on `docker compose run`) is dropped and the re-wait
#      loop hangs. `trap - INT QUIT` in the wrapper restores the default disposition
#      before exec, so Python installs its normal handlers again.
# `exec` makes CHILD_PID the Python process itself, so on_signal's relay reaches it.
# shellcheck disable=SC2086 -- GFMY_ARGS is an intentional word-split flag list.
( trap - INT QUIT; exec python3 main.py ${GFMY_ARGS:-} ) <&0 &
CHILD_PID=$!

# Capture the child's status without tripping `set -e`, then propagate it via the
# final `exit` so the EXIT trap runs cleanup() (ownership handoff to the host user,
# then supervisor shutdown) exactly once on every path: success, nonzero exit, or
# termination. The signal-interrupted re-wait lives in wait_for_tracked_child.
_rc=0
wait_for_tracked_child || _rc=$?
exit_if_terminating "${_rc}"

# --------------------------------------------------------------------------
# Post-login handoff tracks (only after a successful main.py run; unset -> the
# historical behaviour is completely unchanged).
#
# GFMY_ONECLICK and GFMY_CLEARTEXT are INDEPENDENT switches (as documented in
# docker-compose.yml and README.md), so the two blocks below are two separate
# `if`s evaluated in sequence, never an if/elif chain: with both set, Track C
# has to stay reachable after the token server returned. Each block re-tests
# `-f "${_secrets_path}"`, which is what keeps the sequence honest -- the server
# consumes (deletes) the file on ack and on TTL, so Track C only ever prints a
# bundle that is still there -- chiefly the lockout case, and likewise anything
# else that leaves the file behind (a failed delete, for instance) -- never an
# empty block.
#
# These run BLOCKING (the children themselves are backgrounded and tracked, see
# below) *before* the final `exit`, so the EXIT trap's
# cleanup (ownership handoff of /data + supervisor shutdown) fires strictly
# AFTER the handoff is done. That is the BLOCKING-1 lifecycle fix: a One-Click
# token server that must outlive main.py cannot hang off the EXIT trap, so it
# runs here, and cleanup's /data ownership handoff is naturally deferred until
# it returns (ack-delete or TTL). The `secrets.json` the server reads still
# exists at this point (main.py wrote it; cleanup has not run yet).
# --------------------------------------------------------------------------
_secrets_path="${GOOGLEFINDMY_SECRETS_PATH:-/data/secrets.json}"

if [ "${_rc}" -eq 0 ] && [ "${GFMY_ONECLICK:-}" = "1" ] && [ -f "${_secrets_path}" ]; then
  # Track B: hand the freshly minted bundle to Home Assistant over a one-shot,
  # nonce-authenticated, loopback-only endpoint (see token_server.py). The
  # pairing code is generated at RUNTIME (never a compose default) and printed
  # prominently; only the code is shown, never the token/bundle.
  GFMY_PAIRING_CODE="$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')"
  export GFMY_PAIRING_CODE GOOGLEFINDMY_SECRETS_PATH="${_secrets_path}"
  print_oneclick_banner
  # Backgrounded and tracked, for the SAME reason main.py is (see the launch
  # comment above): with a FOREGROUND server bash would defer its TERM/INT traps
  # for up to the full TTL, so a `docker stop` during the wait would hit SIGKILL
  # after the (default 10 s) grace period and the EXIT cleanup would never run --
  # leaving the 0600 bundle owned by the container UID and supervisor unstopped.
  # `trap - INT QUIT` restores Python's default SIGINT disposition in the async
  # child so token_server.py's KeyboardInterrupt path stays reachable; no `<&0`
  # here, the server never reads stdin.
  #
  # This blocks until one of four things happens. In the first three we fall
  # through to the final exit -> EXIT trap cleanup (ownership handoff +
  # supervisor stop):
  #   - Home Assistant acks the handoff  -> secrets.json is deleted (consumed),
  #   - the TTL elapses without an ack   -> secrets.json is deleted (fallback),
  #   - the pairing code is locked out   -> the endpoint closes but secrets.json
  #     is KEPT on purpose, so no login has to be repeated: the file handoff
  #     (Track A) still works, and if GFMY_CLEARTEXT is also set the next block
  #     prints the bundle. Note that the two are alternatives, not cumulative:
  #     Track C is ephemeral by contract and removes the file after printing it,
  #     so with both switches the clear-text output REPLACES the file handoff.
  # The fourth is a terminating signal, and it does NOT fall through: see the
  # check right after the wait. secrets.json is then left on disk unless the
  # server had already consumed it, which is the safe
  # direction -- no login has to be repeated, and the EXIT trap still hands the
  # file's ownership back to the host user.
  ( trap - INT QUIT; exec python3 /app/gfmy/docker-login/token_server.py ) &
  CHILD_PID=$!
  _srv_rc=0
  wait_for_tracked_child || _srv_rc=$?
  # The server dying because we are shutting down is NOT "the server finished":
  # without this the clear-text track below would dump the whole bundle into the
  # container log on a `docker stop`. token_server.py installs no SIGTERM handler,
  # so the file it would otherwise have consumed is still there and that `-f` test
  # would pass.
  exit_if_terminating "${_srv_rc}"
fi

# Deliberately a fresh `if`, not an `elif` on the block above: the switches are
# independent, so with GFMY_ONECLICK=1 *and* GFMY_CLEARTEXT=1 this is the
# promised fallback for the lockout case, where the server left secrets.json in
# place on purpose. The `-f` test is re-evaluated AFTER the server returned, so
# an acked or TTL-expired (i.e. deleted) bundle prints nothing at all. The
# clear-text block exists exactly once and serves both entry paths.
if [ "${_rc}" -eq 0 ] && [ "${GFMY_CLEARTEXT:-}" = "1" ] && [ -f "${_secrets_path}" ]; then
  # Track C: no port is opened. Print the full secrets.json in a clearly
  # delimited block on THIS script's stdout, i.e. in the terminal that runs the
  # launcher (or in `docker logs`). That is a different sink from the noVNC
  # viewer, which renders the X display served by supervisord and never sees
  # this file descriptor; a terminal opened inside the desktop is a fresh shell
  # under supervisord, not this output stream. Select and copy the block there
  # and paste it into Home Assistant's secrets.json field. The file is ephemeral
  # and removed right after display so nothing lingers on disk.
  echo ""
  echo "=================== BEGIN secrets.json (copy) ===================="
  cat "${_secrets_path}"
  echo ""
  echo "==================== END secrets.json (copy) ====================="
  echo "[entrypoint] Select the block above, copy it, and paste it into the"
  echo "[entrypoint] Home Assistant 'secrets.json' field."
  echo ""
  # Report the real outcome instead of asserting it. A silenced `rm` claimed the
  # bundle was gone even when it was not (a readable file under a directory that
  # is no longer writable), leaving the full credentials on disk with no warning
  # -- the opposite of what Track C promises.
  if rm -f "${_secrets_path}" 2>/dev/null; then
    echo "[entrypoint] The file has been removed."
  else
    echo "[entrypoint] WARNING: could not remove ${_secrets_path}." >&2
    echo "[entrypoint] The full credential bundle is STILL on disk at that path." >&2
    echo "[entrypoint] Delete it yourself before leaving this host unattended." >&2
  fi
fi

exit "${_rc}"
