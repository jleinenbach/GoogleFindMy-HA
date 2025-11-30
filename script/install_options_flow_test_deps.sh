#!/bin/sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REQ_FILE="$ROOT_DIR/requirements-options-flow-tests.txt"

if [ ! -f "$REQ_FILE" ]; then
    echo "Unable to locate requirements file: $REQ_FILE" >&2
    exit 1
fi

python3 -m pip install --upgrade pip
python3 -m pip install -r "$REQ_FILE"
