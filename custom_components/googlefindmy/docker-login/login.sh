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
#      login CLI can ask you for the account e-mail if it needs to.
#
# THREE ADDRESS ROLES, deliberately separate (do not merge them again). They
# exist because the two published ports serve different consumers: 7901 is
# machine-to-machine (Home Assistant on this host), 7900 is opened by a browser
# that usually runs on a DIFFERENT machine than the Docker host.
#
#   GFMY_ONECLICK_BIND     host address the token endpoint (7901) is published
#                          on. Default 127.0.0.1, which Home Assistant reaches
#                          when it shares this host's network namespace (HAOS,
#                          HA Core, `network_mode: host`). HA in a bridge network
#                          has its OWN loopback and cannot, so for that case name
#                          a LAN address of this host here (the track menu offers
#                          it), or use the shared-network route in README.md,
#                          which publishes no host port at all, or the file
#                          handoff. That endpoint serves Google credentials in
#                          CLEARTEXT, so the host publish is the boundary: the
#                          default never widens by itself, a wildcard is refused
#                          outright, and a LAN value is warned about before the
#                          container starts.
#   GFMY_NOVNC_BIND        host address noVNC (7900) binds to. Default 127.0.0.1.
#   GFMY_NOVNC_URL_HOST    address PRINTED for you to open in a browser. Defaults
#                          to GFMY_NOVNC_BIND. A wildcard value, whether it comes
#                          from the bind or from this variable, is replaced by
#                          the first detected address: "0.0.0.0" is a bind
#                          pattern, not a browsable address.
#
# On loopback (the default) noVNC uses the base image's fixed password "secret".
# A LAN bind hardens it: the container mints a per-run password and (unless
# --no-tls) serves self-signed HTTPS. To reach it from another machine, either
# tunnel
#   ssh -L 7900:127.0.0.1:7900 <docker-host>
# or, only on a trusted LAN, bind it to a CONCRETE address (preferred over the
# 0.0.0.0 wildcard, which publishes on every interface):
#   bash login.sh --ip 192.168.1.21
#
# Optional one-click handoff:
#   GFMY_ONECLICK=1 bash login.sh        (or answer B to the menu, or --track b)
# only then does this script add docker-compose.oneclick.yml, which publishes the
# token endpoint on port 7901. Without it no 7901 port is published at all, so the
# file handoff and GFMY_CLEARTEXT=1 still start on a host where port 7901 is
# already in use. The host side of that publish defaults to 127.0.0.1 and is
# widened only on request, through GFMY_ONECLICK_BIND or the address question the
# menu asks for track B. Widening it puts CLEAR-TEXT credentials on the wire for
# the duration of the handoff: LAN or same host only, never an untrusted network.
set -euo pipefail

cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
Usage: bash login.sh [--ip <ADDRESS>] [--track a|b|c] [--help]

  --ip <ADDRESS>  Bind the noVNC viewer (port 7900) to <ADDRESS> and print that
                  address as the URL to open. Use a concrete LAN address of this
                  Docker host, for example: bash login.sh --ip 192.168.1.21
  --no-tls        On a LAN bind, do NOT enable the self-signed TLS viewer; serve
                  a plain http viewer instead (e.g. when you tunnel it yourself).
                  Ignored for a loopback bind, which is plain either way.
  --track a|b|c   Pick how the finished credentials reach Home Assistant, without
                  being asked: a = file only (default), b = also publish the
                  one-shot token endpoint on 7901, c = also print the bundle in
                  this terminal. Equivalent to GFMY_ONECLICK / GFMY_CLEARTEXT.
  --help          Show this help and exit.

Without --ip and without GFMY_NOVNC_BIND/GFMY_NOVNC_URL_HOST, and only on an
interactive terminal, the launcher asks which address to open the noVNC viewer
on (a numbered menu of detected LAN addresses, a loopback entry, or a free-text
IP). A non-interactive run keeps the historical loopback default unchanged.

The same rule applies to the handoff track: on an interactive terminal, and only
when neither --track nor GFMY_ONECLICK/GFMY_CLEARTEXT was given, the launcher
asks which way you want. Track A (the file in ./data) always runs, so a bare
Enter keeps exactly the historical behaviour.

Environment (see the comment block at the top of this file):
  GFMY_NOVNC_BIND       host bind for noVNC 7900         (default 127.0.0.1)
  GFMY_NOVNC_URL_HOST   address printed for your browser (default: the bind)
  GFMY_ONECLICK=1       add the opt-in overlay that publishes port 7901
  GFMY_CLEARTEXT=1      print the bundle in this terminal at the end
  GFMY_ONECLICK_BIND    host bind for the token endpoint 7901 (default 127.0.0.1)

The token endpoint (7901) defaults to 127.0.0.1 and is only widened when you ask
for it, because it serves the credentials UNENCRYPTED: keep it on this host or a
trusted LAN, and only for the few seconds the handoff takes. A wildcard bind
(0.0.0.0, ::, *) is refused for that port.
EOF
}

# Capture whether the operator pinned the bind / printed host through the
# environment BEFORE the loopback default is applied, so the interactive menu
# (AP-1) can tell "nothing was set" from "set to the default value". The `+set`
# form is true only when the name is defined (empty or not) and is safe under
# `set -u`.
novnc_bind_env_set=0
[ -n "${GFMY_NOVNC_BIND+set}" ] && novnc_bind_env_set=1
novnc_url_host_env_set=0
[ -n "${GFMY_NOVNC_URL_HOST+set}" ] && novnc_url_host_env_set=1

# Same capture for the handoff switches (AP-5). The interactive track menu must
# be able to tell "the operator said nothing" from "the operator said off", so a
# deliberate GFMY_ONECLICK=0 keeps suppressing the question instead of being
# re-asked on every run.
oneclick_env_set=0
[ -n "${GFMY_ONECLICK+set}" ] && oneclick_env_set=1
cleartext_env_set=0
[ -n "${GFMY_CLEARTEXT+set}" ] && cleartext_env_set=1
oneclick_bind_env_set=0
[ -n "${GFMY_ONECLICK_BIND+set}" ] && oneclick_bind_env_set=1
# Set to 1 by set_track_option when --track is passed, so the menu is skipped for
# the same reason --ip skips the address menu: the operator already chose.
track_from_cli=0

novnc_bind="${GFMY_NOVNC_BIND:-127.0.0.1}"
novnc_url_host="${GFMY_NOVNC_URL_HOST:-}"
# Set to 1 by set_ip_option when --ip is passed: the interactive menu is then
# skipped because the operator already chose. tls_enabled defaults on and is
# turned off by --no-tls (LAN transport opt-out for people who tunnel/plain).
ip_from_cli=0
tls_enabled=1

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
  ip_from_cli=1
}

# --track a|b|c: the non-interactive spelling of the handoff menu below. It sets
# the SAME two switches the menu sets, so there is exactly one signal path into
# the container (entrypoint.sh keeps comparing against the literal "1"). Track A
# is not a switch at all: the file in ./data is written on every run, so "a"
# means "turn the other two off", which is also what the historical default did.
set_track_option() {
  case "$1" in
    a | A)
      GFMY_ONECLICK=""
      GFMY_CLEARTEXT=""
      ;;
    b | B)
      GFMY_ONECLICK=1
      GFMY_CLEARTEXT=""
      ;;
    c | C)
      GFMY_ONECLICK=""
      GFMY_CLEARTEXT=1
      ;;
    *)
      echo "[login] --track needs a, b or c, got '$1'." >&2
      echo "[login]   a = file handoff only (./data/secrets.json, the default)" >&2
      echo "[login]   b = also publish the one-shot token endpoint on port 7901" >&2
      echo "[login]   c = also print the bundle in this terminal" >&2
      exit 2
      ;;
  esac
  export GFMY_ONECLICK GFMY_CLEARTEXT
  track_from_cli=1
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
    --track)
      if [ "$#" -lt 2 ]; then
        echo "[login] --track needs a value, e.g. --track a" >&2
        exit 2
      fi
      set_track_option "$2"
      shift 2
      ;;
    --track=*)
      set_track_option "${1#--track=}"
      shift
      ;;
    --no-tls)
      # LAN transport opt-out: keep the plain-http viewer even on a LAN bind
      # (for operators who tunnel themselves or accept plain on a trusted LAN).
      # Has no effect on a loopback bind, which is plain in either case.
      tls_enabled=0
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

# Interactive noVNC address chooser (AP-1). Reached only from a real TTY and only
# when nothing was pinned (no --ip, no GFMY_NOVNC_* env). It offers the detected
# LAN addresses as a numbered menu, plus a loopback entry and a free-text option,
# and validates the result with the SAME is_ip_literal that --ip uses, re-asking
# on a bad entry. Bare Enter takes the most likely LAN address (192.168.x is
# preferred over 10.x over anything else, since a home browser almost always sits
# on the 192.168 side). Sets novnc_bind / novnc_url_host on success.
prompt_for_ip() {
  local ips selected choice default cand _ip idx
  local -a options=()
  ips="$(detect_lan_ips)"
  while IFS= read -r _ip; do
    [ -n "$_ip" ] && options+=("$_ip")
  done <<EOF
$ips
EOF

  default=""
  for cand in ${options[@]+"${options[@]}"}; do
    case "$cand" in 192.168.*) default="$cand"; break ;; esac
  done
  if [ -z "$default" ]; then
    for cand in ${options[@]+"${options[@]}"}; do
      case "$cand" in 10.*) default="$cand"; break ;; esac
    done
  fi
  if [ -z "$default" ] && [ "${#options[@]}" -gt 0 ]; then
    default="${options[0]}"
  fi
  # When nothing was detected, loopback is the only safe default: the operator
  # can still type a concrete address, or accept loopback and tunnel to it.
  [ -n "$default" ] || default="127.0.0.1"

  while true; do
    # The whole menu goes to stderr so it never contaminates the stdout that the
    # behavioural launcher test parses; the read still uses the terminal stdin.
    {
      echo ""
      echo "[login] Where will you open the noVNC viewer? Pick the address your"
      echo "[login] browser can reach on this Docker host:"
      idx=1
      for cand in ${options[@]+"${options[@]}"}; do
        echo "[login]   ${idx}) ${cand}"
        idx=$((idx + 1))
      done
      echo "[login]   L) 127.0.0.1  (loopback only; reach it via an SSH tunnel)"
      echo "[login]   or type any other IP address of this host"
      printf '[login] Choice [Enter = %s]: ' "$default"
    } >&2
    read -r choice || choice=""

    if [ -z "$choice" ]; then
      selected="$default"
    elif [ "$choice" = "L" ] || [ "$choice" = "l" ]; then
      selected="127.0.0.1"
    elif printf '%s' "$choice" | grep -qE '^[0-9]+$'; then
      if [ "$choice" -ge 1 ] && [ "$choice" -le "${#options[@]}" ]; then
        selected="${options[$((choice - 1))]}"
      else
        echo "[login] No menu entry ${choice}. Try again." >&2
        continue
      fi
    else
      selected="$choice"
    fi

    if [ -z "$selected" ]; then
      echo "[login] No address available; type one, e.g. 192.168.1.21." >&2
      continue
    fi
    if is_ip_literal "$selected"; then
      novnc_bind="$selected"
      novnc_url_host="$selected"
      return 0
    fi
    echo "[login] '${selected}' is not an IP address of this host. Try again." >&2
  done
}

# Interactive handoff-track chooser (AP-5). Same shape as prompt_for_ip above:
# menu on stderr so the behavioural launcher tests keep parsing a clean stdout,
# read from the terminal, re-ask on a bad answer. It sets only the two switches
# that already exist, so nothing new travels into the container.
#
# Track A is the DEFAULT on a bare Enter, and deliberately so: the file in ./data
# is written on every run anyway, it needs no port, no network and no shared
# secret, and Home Assistant picks it up by itself when it can see that folder.
# B and C are additions on top of A, never replacements for it.
prompt_for_track() {
  local choice
  while true; do
    {
      echo ""
      echo "[login] How should the finished credentials reach Home Assistant?"
      echo "[login]   A) File only: ./data/secrets.json  (this always happens)"
      echo "[login]      Needs: Home Assistant can see that folder, e.g. this repo"
      echo "[login]      lives under config/custom_components on the HA machine."
      echo "[login]   B) Also publish the one-shot token endpoint on port 7901"
      echo "[login]      Needs: Home Assistant can reach this host over the network."
      echo "[login]      You will be asked which address it should use."
      echo "[login]   C) Also print the bundle in this terminal"
      echo "[login]      Last resort: the credentials then sit in your scrollback,"
      echo "[login]      in 'docker logs' and in the clipboard you paste them from."
      printf '[login] Choice [Enter = A]: '
    } >&2
    read -r choice || choice=""
    case "$choice" in
      "" | a | A)
        return 0
        ;;
      b | B)
        GFMY_ONECLICK=1
        export GFMY_ONECLICK
        return 0
        ;;
      c | C)
        GFMY_CLEARTEXT=1
        export GFMY_CLEARTEXT
        return 0
        ;;
      *)
        echo "[login] Please answer A, B or C (bare Enter picks A)." >&2
        ;;
    esac
  done
}

# Which host address the token endpoint (7901) is published on (AP-6). Until now
# this was pinned to loopback in docker-compose.oneclick.yml, which made track B
# useful only where track A already works: a Home Assistant in its own container,
# or on another machine, cannot reach the Docker host's loopback. The bind is now
# a variable with a LOOPBACK DEFAULT, so an unattended run stays byte-identical,
# and a widening is always a deliberate act.
#
# Precedence, highest first:
#   1. an explicitly set GFMY_ONECLICK_BIND (also the only way in a script/CI),
#   2. on a terminal, the answer to the question below, prefilled with the
#      address noVNC already uses (the "same IP as the viewer" case),
#   3. 127.0.0.1.
# A wildcard is REFUSED rather than warned about: for the noVNC viewer a wildcard
# only widens a password-gated viewer, but this port hands out the credentials
# themselves, and a concrete address can always replace it.
oneclick_bind="127.0.0.1"

reject_wildcard_bind() {
  echo "[login] GFMY_ONECLICK_BIND='$1' is a wildcard, which is refused for the" >&2
  echo "[login] token endpoint: port 7901 serves the Google credentials in clear" >&2
  echo "[login] text, so it must not listen on every interface. Name the one" >&2
  echo "[login] address Home Assistant uses, e.g. GFMY_ONECLICK_BIND=192.168.1.21," >&2
  echo "[login] or leave it unset for 127.0.0.1." >&2
  exit 2
}

prompt_for_oneclick_bind() {
  local choice
  while true; do
    {
      echo ""
      echo "[login] Which address of this host will Home Assistant use to fetch the"
      echo "[login] credentials from port 7901?"
      echo "[login]   Enter = ${oneclick_bind}"
      echo "[login]   L) 127.0.0.1  (only Home Assistant ON this host, or an SSH tunnel)"
      echo "[login]   or type a LAN address of this host, e.g. 192.168.1.21"
      printf '[login] Address [Enter = %s]: ' "$oneclick_bind"
    } >&2
    read -r choice || choice=""
    case "$choice" in
      "")
        return 0
        ;;
      L | l)
        oneclick_bind="127.0.0.1"
        return 0
        ;;
    esac
    if is_wildcard_addr "$choice"; then
      echo "[login] A wildcard is not accepted here: 7901 serves the credentials in" >&2
      echo "[login] clear text. Name the single address Home Assistant uses." >&2
      continue
    fi
    if is_ip_literal "$choice"; then
      oneclick_bind="$(bracket_if_ipv6 "$choice")"
      return 0
    fi
    echo "[login] '${choice}' is not an IP address of this host. Try again." >&2
  done
}

# Best-effort: say up front that 7901 is taken, instead of letting `docker
# compose` fail with an opaque "port is already allocated" after the build. Every
# probe is optional; on a host without ss/netstat we simply stay quiet rather
# than claim anything. `|| true` keeps `set -e`/`pipefail` out of it.
warn_if_token_port_busy() {
  local listeners=""
  if command -v ss >/dev/null 2>&1; then
    listeners="$(ss -ltn 2>/dev/null | grep -E '[.:]7901[[:space:]]' || true)"
  elif command -v netstat >/dev/null 2>&1; then
    listeners="$(netstat -ltn 2>/dev/null | grep -E '[.:]7901[[:space:]]' || true)"
  else
    return 0
  fi
  [ -n "$listeners" ] || return 0
  echo "[login] WARNING: something already listens on port 7901 of this host, so" >&2
  echo "[login] the container may fail to start with 'port is already allocated'." >&2
  echo "[login] Either free that port, or use the file handoff (track A), which" >&2
  echo "[login] needs no port at all: bash login.sh --track a" >&2
}

# Interactive address selection (AP-1): only on a real terminal AND only when the
# operator pinned nothing explicitly (no --ip, no GFMY_NOVNC_BIND/URL_HOST). In
# CI or any non-interactive pipe `[ -t 0 ]` is false, so this is skipped and the
# historical loopback default below is reached completely UNCHANGED.
if [ -t 0 ] && [ "${ip_from_cli}" -eq 0 ] \
   && [ "${novnc_bind_env_set}" -eq 0 ] && [ "${novnc_url_host_env_set}" -eq 0 ]; then
  prompt_for_ip
fi

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
# Also export the browsable host so docker-compose can build the noVNC URL that
# the in-container login flow prints in its instruction block. This is the
# fully normalised value (wildcards resolved to a concrete address, IPv6
# bracketed), i.e. exactly what belongs in a URL. Re-exporting under the same
# input name is safe: it is read once at startup (above) and only consumed by
# child processes (docker compose) from here on.
export GFMY_NOVNC_URL_HOST="$novnc_url_host"

# Handoff track (AP-5) and, for track B, the address of the token endpoint
# (AP-6). Placed AFTER the noVNC normalisation on purpose: track B prefills its
# address with the fully resolved noVNC bind, which is what "the same IP as the
# viewer" means. The gate mirrors the address menu above -- a real terminal, and
# nothing pinned by --track or by GFMY_ONECLICK/GFMY_CLEARTEXT -- so CI, a pipe
# and every documented `GFMY_ONECLICK=1 bash login.sh` invocation reach the code
# below completely UNCHANGED.
if [ -t 0 ] && [ "${track_from_cli}" -eq 0 ] \
   && [ "${oneclick_env_set}" -eq 0 ] && [ "${cleartext_env_set}" -eq 0 ]; then
  prompt_for_track
  track_from_prompt=1
else
  track_from_prompt=0
fi

if [ "${oneclick_bind_env_set}" -eq 1 ]; then
  # An explicit value wins over everything, including the noVNC address. It is
  # also the only route in a non-interactive run, so it gets the full validation
  # that --ip gets: an unusable value must fail HERE, not inside docker.
  oneclick_bind="${GFMY_ONECLICK_BIND:-127.0.0.1}"
  [ -n "$oneclick_bind" ] || oneclick_bind="127.0.0.1"
  if is_wildcard_addr "$oneclick_bind"; then
    reject_wildcard_bind "$oneclick_bind"
  fi
  if ! is_ip_literal "$oneclick_bind"; then
    echo "[login] GFMY_ONECLICK_BIND needs an IP address of this Docker host, got" >&2
    echo "[login] '${oneclick_bind}'. Host names are not accepted: the value becomes" >&2
    echo "[login] a docker port bind. Example: GFMY_ONECLICK_BIND=192.168.1.21" >&2
    exit 2
  fi
  oneclick_bind="$(bracket_if_ipv6 "$oneclick_bind")"
elif [ "${GFMY_ONECLICK:-}" = "1" ] && [ "${track_from_prompt}" -eq 1 ]; then
  # Track B chosen at the prompt: offer the viewer's address, since that is the
  # address the operator just confirmed this host is reachable on. A wildcard
  # viewer bind is not a reachable address, so it falls back to loopback.
  oneclick_bind="$novnc_bind"
  if is_wildcard_addr "$oneclick_bind"; then
    oneclick_bind="127.0.0.1"
  fi
  prompt_for_oneclick_bind
fi
export GFMY_ONECLICK_BIND="$oneclick_bind"

mkdir -p data

# Hand the finished secrets.json back to the invoking host user as owner (0600).
# The container chowns ./data to itself for the run, then back to these IDs.
export GFMY_HOST_UID="$(id -u)"
export GFMY_HOST_GID="$(id -g)"

# LAN-bind hardening (AP-7 + AP-8). A non-loopback bind exposes the noVNC viewer
# on the network, where the base image's public default password "secret" and the
# plain-HTTP transport are both inadequate. For that case ONLY, raise a single
# verdict, GFMY_NOVNC_HARDEN=1, that the CONTAINER acts on: entrypoint.sh mints a
# per-run dense random password (SE_VNC_PASSWORD, fed to x11vnc by the base image's
# start-vnc.sh) IN THE CONTAINER, so this launcher does no crypto and login.cmd on
# Windows gets the exact same hardening. Unless --no-tls was given, also raise
# GFMY_NOVNC_TLS=1 so entrypoint.sh switches the viewer to a self-signed TLS
# transport (realised in entrypoint.sh + start-novnc.sh, SAN = the chosen IP). A
# loopback bind keeps the historical "secret"/plain behaviour: nothing on the
# network can reach it, so the extra typing would be pure friction. The keying is
# on the BIND, mirroring the reachability hint below, so a wildcard bind is treated
# as LAN too.
novnc_scheme="http"
# Clear both verdicts first, so a value inherited from the caller's environment
# cannot harden a loopback run (which must stay byte-for-byte the base default).
# They are re-raised below only for a real LAN bind.
export GFMY_NOVNC_HARDEN=""
export GFMY_NOVNC_TLS=""
if ! is_loopback_addr "$novnc_bind"; then
  export GFMY_NOVNC_HARDEN=1
  if [ "$tls_enabled" -eq 1 ]; then
    export GFMY_NOVNC_TLS=1
    novnc_scheme="https"
  fi
fi

echo "[login] Starting the GoogleFindMy-HA login container..."
if is_loopback_addr "$novnc_bind"; then
  # Loopback keeps the base image's known fixed password, so it is safe -- and
  # helpful -- to print it here.
  echo "[login] When it is up, open ${novnc_scheme}://${novnc_url_host}:7900 (password: secret)"
else
  # LAN bind: the per-run password is minted INSIDE the container, so the host does
  # not know it and cannot print it. It appears in the container output further
  # down, both in the '[entrypoint] noVNC available at ...' line and in the
  # '[AuthFlow]' sign-in banner, each as '(password: ...)'.
  echo "[login] When it is up, open ${novnc_scheme}://${novnc_url_host}:7900"
  echo "[login] (the per-run noVNC password is printed by the container below, as"
  echo "[login] '(password: ...)' in the noVNC-available line and the [AuthFlow] banner)."
fi
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
  echo "[login] beyond this host. For this LAN bind the container replaces the base"
  echo "[login] image's public password with a per-run password (printed in the"
  echo "[login] container output below, not here: the host never sees it),"
  if [ "$tls_enabled" -eq 1 ]; then
    echo "[login] and encrypts the viewer with a self-signed certificate: open the"
    echo "[login] https URL above and accept the one-time browser warning. Use it on"
    echo "[login] a trusted LAN only, and only while logging in."
  else
    echo "[login] but TLS is OFF (--no-tls), so the transport is plain http. Use it"
    echo "[login] on a trusted LAN only, and only while logging in."
  fi
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
  warn_if_token_port_busy
  echo "[login] One-click enabled: token endpoint published on ${oneclick_bind} port 7901."
  if is_loopback_addr "$oneclick_bind"; then
    echo "[login] That is this host's loopback, so only a Home Assistant sharing this"
    echo "[login] host's network namespace reaches it (HAOS, HA Core, network_mode:"
    echo "[login] host). A Home Assistant in its own bridge container or on another"
    echo "[login] machine does not. For those, name a LAN address of this host:"
    echo "[login]   GFMY_ONECLICK_BIND=<ADDRESS> bash login.sh"
    echo "[login] or tunnel to it: ssh -L 7901:127.0.0.1:7901 <docker-host>"
  else
    # Hardening verdict for 7901, mirroring the noVNC block above -- except that
    # there is no TLS counterpart here: the Home Assistant client speaks plain
    # http on purpose, which was harmless while the publish could not leave the
    # host. Once it can, saying so out loud is the whole mitigation.
    echo "[login] WARNING: that address is reachable beyond this host, and the token"
    echo "[login] endpoint serves your Google credentials UNENCRYPTED (plain http)."
    echo "[login] Guarded only by a one-time pairing code, a 300-second expiry, a"
    echo "[login] single successful fetch and a lockout after five wrong codes."
    echo "[login] Use it for that short handoff only, and only inside your own LAN"
    echo "[login] or on this host: never across an untrusted network or the internet."
  fi
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
# terminal attached so the login CLI can prompt for the account e-mail.
# `--service-ports` publishes the ports declared by the selected compose files.
docker compose "${compose_files[@]}" run --build --service-ports --rm googlefindmy-login
