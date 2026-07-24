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
# THREE ADDRESS ROLES, deliberately separate (do not merge them again). They
# exist because the two published ports serve different consumers: 7901 is
# machine-to-machine (Home Assistant on this host), 7900 is opened by a browser
# that usually runs on a DIFFERENT machine than the Docker host.
#
#   token endpoint (7901)  NOT configurable, pinned to 127.0.0.1 in
#                          docker-compose.oneclick.yml. That pin is the no-LAN
#                          guarantee for an endpoint that serves Google
#                          credentials in cleartext, which therefore gets no
#                          environment override surface at all: one stray
#                          variable must not be able to widen it. Home Assistant
#                          reaches it when it shares this host's network namespace
#                          (HAOS, HA Core, `network_mode: host`). HA in a bridge
#                          network has its OWN loopback and cannot; that case is
#                          served by the shared-network route in README.md,
#                          which publishes no host port at all, or by the file
#                          handoff.
#   GFMY_NOVNC_BIND        host address noVNC (7900) binds to. Default 127.0.0.1.
#   GFMY_NOVNC_URL_HOST    address PRINTED for you to open in a browser. Defaults
#                          to GFMY_NOVNC_BIND. A wildcard value, whether it comes
#                          from the bind or from this variable, is replaced by
#                          the first detected address: "0.0.0.0" is a bind
#                          pattern, not a browsable address.
#
# noVNC uses the base image's fixed password "secret". To reach it from another
# machine, either tunnel
#   ssh -L 7900:127.0.0.1:7900 <docker-host>
# or, only on a trusted LAN, bind it to a CONCRETE address (preferred over the
# 0.0.0.0 wildcard, which publishes on every interface):
#   bash login.sh --ip 192.168.1.21
#
# Optional one-click handoff:
#   GFMY_ONECLICK=1 bash login.sh
# only then does this script add docker-compose.oneclick.yml, which publishes the
# token endpoint on host loopback (127.0.0.1, port 7901). Without it no 7901 port
# is published at all, so the file handoff and GFMY_CLEARTEXT=1 still start on a
# host where port 7901 is already in use.
set -euo pipefail

cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
Usage: bash login.sh [--ip <ADDRESS>] [--help]

  --ip <ADDRESS>  Bind the noVNC viewer (port 7900) to <ADDRESS> and print that
                  address as the URL to open. Use a concrete LAN address of this
                  Docker host, for example: bash login.sh --ip 192.168.1.21
  --help          Show this help and exit.

Environment (see the comment block at the top of this file):
  GFMY_NOVNC_BIND       host bind for noVNC 7900         (default 127.0.0.1)
  GFMY_NOVNC_URL_HOST   address printed for your browser (default: the bind)
  GFMY_ONECLICK=1       add the opt-in overlay that publishes port 7901

The token endpoint (7901) is not configurable here: it is pinned to 127.0.0.1
in docker-compose.oneclick.yml, on purpose.
EOF
}

novnc_bind="${GFMY_NOVNC_BIND:-127.0.0.1}"
novnc_url_host="${GFMY_NOVNC_URL_HOST:-}"

is_ip_literal() {
  # Accept a dotted quad with in-range octets, or an (optionally bracketed)
  # IPv6 literal. A hostname is rejected on purpose: it would be handed to
  # `docker compose` as a host bind and fail there with an opaque publish error,
  # long after we already printed it as a working URL.
  local inner
  case "$1" in
    *:*)
      case "$1" in
        *[!0-9A-Fa-f:\[\]]*) return 1 ;;
      esac
      # Brackets, if present at all, must be exactly one enclosing pair.
      # Otherwise `[::1` would be accepted and bracket_if_ipv6 would turn it
      # into `[[::1]`, so "normalise to exactly one pair" would be a lie.
      case "$1" in
        \[*\]) inner="${1#\[}"; inner="${inner%\]}" ;;
        *\[* | *\]*) return 1 ;;
        *) inner="$1" ;;
      esac
      case "$inner" in
        *\[* | *\]* | "") return 1 ;;
      esac
      # Minimal IPv6 structure. The character allowlist alone would accept a
      # lone ":" and hand it to docker as a port bind, so require either a "::"
      # run or at least two separators, and forbid a ":::" run.
      case "$inner" in
        *:::*) return 1 ;;
      esac
      # An empty group at either end must be caught HERE, on the raw string,
      # not inferred from the split below: word splitting on IFS=":" drops a
      # single trailing separator, and the counting loop skips empty fields, so
      # ":1:2:3:4:5:6:7:8" and "1:2:3:4:5:6:7:8:" would both produce eight
      # non-empty groups and satisfy the "exactly eight" rule. A leading or
      # trailing ":" is legal only as part of a "::" run ("::1", "1::", "::").
      case "$inner" in
        ::*) ;;
        :*) return 1 ;;
      esac
      case "$inner" in
        *::) ;;
        *:) return 1 ;;
      esac
      case "$inner" in
        *::* | *:*:*) ;;
        *) return 1 ;;
      esac
      # Something addressable has to be in there; "::" itself is the one
      # exception (the unspecified address, handled as a wildcard elsewhere).
      case "$inner" in
        "::") ;;
        *[0-9A-Fa-f]*) ;;
        *) return 1 ;;
      esac
      # At most ONE "::" run: two of them are ambiguous, so RFC 4291 forbids it.
      local has_compression=0 tail_after_run
      case "$inner" in
        *::*)
          has_compression=1
          tail_after_run="${inner#*::}"
          case "$tail_after_run" in
            *::*) return 1 ;;
          esac
          ;;
      esac
      # Group count and width. Without compression exactly eight groups are
      # required; with it at most seven may be written out, since "::" stands
      # for one or more omitted groups.
      local saved_ifs=$IFS group groups=0
      local -a parts=()
      IFS=:
      # shellcheck disable=SC2206  # word splitting on IFS is exactly the intent
      parts=($inner)
      IFS=$saved_ifs
      for group in ${parts[@]+"${parts[@]}"}; do
        [ -n "$group" ] || continue
        [ "${#group}" -le 4 ] || return 1
        groups=$((groups + 1))
      done
      if [ "$has_compression" -eq 1 ]; then
        [ "$groups" -le 7 ] || return 1
      else
        [ "$groups" -eq 8 ] || return 1
      fi
      return 0
      ;;
  esac
  # Structure BEFORE splitting, for the same reason as in the IPv6 branch: word
  # splitting on IFS="." drops a single trailing separator, so "1.2.3.4." would
  # yield four clean octets and pass the count check. A leading dot, a trailing
  # dot and an inner ".." all mean an empty octet and are rejected on the raw
  # string, where the emptiness is still visible.
  case "$1" in
    *[!0-9.]* | "" | *..* | .* | *.) return 1 ;;
  esac
  local IFS=.
  # shellcheck disable=SC2206  # word splitting on IFS is exactly the intent
  local -a octets=($1)
  [ "${#octets[@]}" -eq 4 ] || return 1
  local octet
  for octet in "${octets[@]}"; do
    [ -n "$octet" ] || return 1
    [ "$octet" -le 255 ] 2>/dev/null || return 1
  done
  return 0
}

bracket_if_ipv6() {
  # An IPv6 literal needs brackets in a URL (otherwise the port cannot be told
  # from the address) and in a docker port publish. `is_ip_literal` accepts the
  # bare and the bracketed spelling alike, so normalise to exactly one pair.
  case "$1" in
    *:*)
      case "$1" in
        \[*\]) printf '%s' "$1" ;;
        *) printf '[%s]' "$1" ;;
      esac
      ;;
    *) printf '%s' "$1" ;;
  esac
}

is_wildcard_addr() {
  case "$1" in
    0.0.0.0 | :: | "[::]" | "*") return 0 ;;
  esac
  return 1
}

is_loopback_addr() {
  case "$1" in
    127.* | ::1 | "[::1]" | localhost) return 0 ;;
  esac
  return 1
}

set_ip_option() {
  if [ -z "$1" ]; then
    echo "[login] --ip needs an address, e.g. --ip 192.168.1.21" >&2
    exit 2
  fi
  if ! is_ip_literal "$1"; then
    echo "[login] --ip needs an IP address of this Docker host, got '$1'." >&2
    echo "[login] Host names are not accepted here: the value becomes a docker" >&2
    echo "[login] port bind. Example: --ip 192.168.1.21" >&2
    exit 2
  fi
  novnc_bind="$1"
  novnc_url_host="$1"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ip)
      if [ "$#" -lt 2 ]; then
        echo "[login] --ip needs an address, e.g. --ip 192.168.1.21" >&2
        exit 2
      fi
      set_ip_option "$2"
      shift 2
      ;;
    --ip=*)
      set_ip_option "${1#--ip=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      echo "[login] Unknown option: '$1'" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# Best-effort LAN address discovery for the printed noVNC URL. Every tool here is
# optional: on a host without any of them we simply fall back to the loopback
# default instead of failing. `|| true` keeps `set -e`/`pipefail` from aborting
# when a filter matches nothing.
detect_lan_ips() {
  # Emitted as "<interface> <address>" so container/VPN interfaces can be
  # dropped by NAME. Filtering by address range instead would also hide a
  # genuine 172.16/12 LAN, which is a worse error than showing one address too
  # many: docker0/br-*/veth* are exactly the addresses a browser cannot reach.
  {
    if command -v ip >/dev/null 2>&1; then
      ip -4 -o addr show scope global 2>/dev/null |
        awk '{split($4, a, "/"); print $2, a[1]}'
    elif command -v ifconfig >/dev/null 2>&1; then
      ifconfig 2>/dev/null | awk '
        /^[a-zA-Z0-9._-]+[: ]/ { name = $1; sub(/:$/, "", name) }
        /[ \t]inet / { addr = $2; sub(/^addr:/, "", addr); print name, addr }'
    elif command -v hostname >/dev/null 2>&1; then
      hostname -I 2>/dev/null | tr ' ' '\n' | sed 's/^/unknown /'
    fi
  } | awk '$2 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print}' \
    | grep -vE '^(docker|br-|veth|virbr|tun|tap|wg|zt|tailscale)' \
    | awk '{print $2}' \
    | grep -vE '^(127\.|169\.254\.)' \
    | sort -u \
    || true
}

# The printed address is derived AFTER all parsing, so --ip and both environment
# variables run through the same normalisation. Deriving it inside the "nothing
# was set" branch would leave the explicit paths unnormalised.
[ -n "$novnc_url_host" ] || novnc_url_host="$novnc_bind"

lan_ips=""
if is_wildcard_addr "$novnc_url_host" || is_loopback_addr "$novnc_bind"; then
  lan_ips="$(detect_lan_ips)"
fi

if is_wildcard_addr "$novnc_url_host" || [ -z "$novnc_url_host" ]; then
  # A wildcard is a bind pattern, never a browsable address: print a concrete
  # one instead, and fall back to loopback when nothing was detected.
  novnc_url_host="$(printf '%s\n' "$lan_ips" | head -n 1)"
  [ -n "$novnc_url_host" ] || novnc_url_host="127.0.0.1"
fi

# Both roles need the brackets: the URL so the port can be told from the
# address, the bind because docker's port syntax demands them for IPv6.
novnc_url_host="$(bracket_if_ipv6 "$novnc_url_host")"
novnc_bind="$(bracket_if_ipv6 "$novnc_bind")"

export GFMY_NOVNC_BIND="$novnc_bind"

mkdir -p data

# Hand the finished secrets.json back to the invoking host user as owner (0600).
# The container chowns ./data to itself for the run, then back to these IDs.
export GFMY_HOST_UID="$(id -u)"
export GFMY_HOST_GID="$(id -g)"

echo "[login] Starting the GoogleFindMy-HA login container..."
echo "[login] When it is up, open http://${novnc_url_host}:7900 (password: secret)"
echo "[login] and log into Google in the browser view."

# Tell the truth about who can actually reach that address. This MUST key on the
# BIND, not on the printed address: with a wildcard bind the printed address is a
# concrete one, and branching on it would claim "only this host" for a viewer
# that is in fact listening on every interface.
if is_loopback_addr "$novnc_bind"; then
  echo "[login] noVNC is bound to ${novnc_bind}, so only this Docker host reaches it."
  echo "[login] From another machine either tunnel it:"
  echo "[login]   ssh -L 7900:127.0.0.1:7900 <docker-host>"
  echo "[login] or re-run bound to a LAN address of this host: bash login.sh --ip <ADDRESS>"
  if [ -n "$lan_ips" ]; then
    echo "[login] Addresses detected on this host (pick the one your browser reaches):"
    printf '%s\n' "$lan_ips" | while IFS= read -r _ip; do
      [ -n "$_ip" ] && echo "[login]   bash login.sh --ip ${_ip}"
    done
  fi
else
  echo "[login] WARNING: noVNC is bound to ${novnc_bind} and is therefore reachable"
  echo "[login] beyond this host, protected only by the base image's fixed password"
  echo "[login] \"secret\". Use it on a trusted LAN only, and only while logging in."
  if is_wildcard_addr "$novnc_bind"; then
    echo "[login] That bind is a wildcard, so the URL above names just one of the"
    echo "[login] interfaces it listens on. Prefer bash login.sh --ip <ADDRESS>."
  fi
fi

# Compose files: the base file publishes only noVNC (7900). The one-click token
# endpoint (7901) lives in an OPT-IN overlay, so a host that already uses 7901
# can never block the file handoff (./data) or the GFMY_CLEARTEXT=1 output,
# which do not need that port. Gate matches entrypoint.sh exactly: "1" means on.
compose_files=(-f docker-compose.yml)
if [ "${GFMY_ONECLICK:-}" = "1" ]; then
  compose_files+=(-f docker-compose.oneclick.yml)
  echo "[login] One-click enabled: token endpoint published on 127.0.0.1 port 7901."
  echo "[login] Home Assistant must reach that address; it does when HA shares this"
  echo "[login] host's network namespace (HAOS, HA Core, network_mode: host)."
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
