#!/usr/bin/env bash
#
# Override of the selenium base image's /opt/bin/start-novnc.sh, installed by
# docker-login/Dockerfile with the same COPY mechanism as gfm-entrypoint.sh
# ([program:novnc] in the base image's supervisor config already points at this
# path, so no supervisor change is needed).
#
# Its ONLY job is to add self-signed TLS to the noVNC viewer WHEN, and only when,
# entrypoint.sh minted a certificate for a concrete LAN bind (AP-8). In every
# other case -- the loopback/tunnel default -- it hands off to the base image's
# ORIGINAL script verbatim, so that path is byte-for-byte unchanged. The
# Dockerfile copies the base script to start-novnc.base.sh before this file
# overwrites the original, so the fallback below is the real base launch, not a
# reproduction that could silently drift from it.
set -e

# The combined cert+key entrypoint.sh writes for a LAN bind. Its mere existence
# is the switch: no file -> plain viewer (delegate to the untouched base script).
GFMY_NOVNC_CERT="${GFMY_NOVNC_CERT:-/home/seluser/gfmy-novnc/combined.pem}"
GFMY_NOVNC_BASE="${GFMY_NOVNC_BASE:-/opt/bin/start-novnc.base.sh}"

if [ ! -f "${GFMY_NOVNC_CERT}" ]; then
  # Loopback/tunnel default: nothing changed, run the untouched base launcher.
  exec "${GFMY_NOVNC_BASE}"
fi

# LAN bind: encrypt the viewer transport. novnc_proxy passes --cert/--key/--ssl-only
# straight through to websockify (upstream-supported). --ssl-only disables plain
# HTTP, so the launcher prints an https:// URL for the chosen IP. The selenium
# image serves noVNC on 7900 and VNC on 5900; honour an override if the base image
# ever sets one. The selenium base uses NO_VNC_PORT (with underscore) for the noVNC
# port, so accept both spellings rather than silently ignoring a base override on
# the TLS path. combined.pem holds both cert and key, so both flags point at it.
NOVNC_PORT="${NOVNC_PORT:-${NO_VNC_PORT:-7900}}"
VNC_PORT="${VNC_PORT:-5900}"
exec /opt/bin/noVNC/utils/novnc_proxy \
  --listen "${NOVNC_PORT}" \
  --vnc "localhost:${VNC_PORT}" \
  --cert "${GFMY_NOVNC_CERT}" \
  --key "${GFMY_NOVNC_CERT}" \
  --ssl-only
