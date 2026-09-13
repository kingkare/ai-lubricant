#!/usr/bin/env bash
# Native no-Docker launcher. It intentionally delegates all dependency and process
# handling to the Python package so exe/supervisord/shell shapes cannot drift.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
export NATIVE_APP_ROOT="${NATIVE_APP_ROOT:-$ROOT}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

exec "${PYTHON:-python3}" -m native_deps.cli "$@"
