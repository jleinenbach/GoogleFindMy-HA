#!/usr/bin/env bash
set -euo pipefail

# bootstrap_ssot_cached.sh
# -------------------------
# Helper script that primes a local wheelhouse with the Home Assistant Single
# Source of Truth (SSoT) dependencies and installs them from the cache. The
# first invocation downloads the wheels into `.wheelhouse/ssot`, while
# subsequent runs reuse the cached artifacts when SKIP_WHEELHOUSE_REFRESH=1
# is provided explicitly.

PYTHON_BIN=${PYTHON:-python3}
WHEELHOUSE_DIR=${WHEELHOUSE:-.wheelhouse/ssot}
SKIP_REFRESH=${SKIP_WHEELHOUSE_REFRESH:-0}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MANIFEST_FILE=${SSOT_MANIFEST:-"${SCRIPT_DIR}/ssot_wheel_manifest.txt"}

SSOT_REQUIREMENTS=(
  "homeassistant"
  "pytest-homeassistant-custom-component"
)

SUPPORT_PACKAGES=(
  "setuptools"
  "wheel"
)

mkdir -p "${WHEELHOUSE_DIR}"

if [[ "${SKIP_REFRESH}" != "1" ]] || ! find "${WHEELHOUSE_DIR}" -mindepth 1 -maxdepth 1 -type f >/dev/null 2>&1; then
  echo "[bootstrap_ssot_cached] Downloading wheels into ${WHEELHOUSE_DIR}" >&2
  "${PYTHON_BIN}" -m pip download \
    --dest "${WHEELHOUSE_DIR}" \
    --exists-action=i \
    "${SSOT_REQUIREMENTS[@]}" \
    "${SUPPORT_PACKAGES[@]}"
else
  echo "[bootstrap_ssot_cached] Reusing cached wheels in ${WHEELHOUSE_DIR}" >&2
fi

if [[ -f "${MANIFEST_FILE}" ]]; then
  mapfile -t EXPECTED_PATTERNS < <(grep -E -v '^\s*(#|$)' "${MANIFEST_FILE}")
  if (( ${#EXPECTED_PATTERNS[@]} )); then
    MISSING=()
    for pattern in "${EXPECTED_PATTERNS[@]}"; do
      if ! find "${WHEELHOUSE_DIR}" -maxdepth 1 -type f -name "${pattern}" -print -quit | grep -q .; then
        MISSING+=("${pattern}")
      fi
    done
    if (( ${#MISSING[@]} )); then
      echo "[bootstrap_ssot_cached] Missing expected wheel(s): ${MISSING[*]}" >&2
      if [[ "${SKIP_REFRESH}" == "1" ]]; then
        echo "[bootstrap_ssot_cached] Set SKIP_WHEELHOUSE_REFRESH=0 to refresh the cache." >&2
      fi
      exit 1
    fi
  fi
fi

echo "[bootstrap_ssot_cached] Installing SSoT requirements from ${WHEELHOUSE_DIR}" >&2
"${PYTHON_BIN}" -m pip install \
  --no-index \
  --find-links "${WHEELHOUSE_DIR}" \
  --upgrade \
  "${SUPPORT_PACKAGES[@]}" \
  "${SSOT_REQUIREMENTS[@]}"
