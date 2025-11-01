#!/usr/bin/env bash
# script/rg_proto_snippet.sh
# Lightweight helper for sampling generated protobuf files without flooding the shell.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: script/rg_proto_snippet.sh PATTERN PATH [PATH...]

Search generated protobuf sources using ripgrep while limiting the output volume.

Environment variables:
  MAX_HITS   Maximum matches to return per file (default: 20)
  WIDTH      Maximum number of characters to display per line (default: 160)
USAGE
}

if [[ $# -lt 2 ]]; then
  usage >&2
  exit 1
fi

MAX_HITS=${MAX_HITS:-20}
WIDTH=${WIDTH:-160}

if ! [[ $MAX_HITS =~ ^[0-9]+$ && $MAX_HITS -gt 0 ]]; then
  echo "MAX_HITS must be a positive integer (received '$MAX_HITS')." >&2
  exit 2
fi

if ! [[ $WIDTH =~ ^[0-9]+$ && $WIDTH -gt 0 ]]; then
  echo "WIDTH must be a positive integer (received '$WIDTH')." >&2
  exit 2
fi

rg --max-count "$MAX_HITS" --line-number "$1" "${@:2}" \
  | cut -c -"$WIDTH"
