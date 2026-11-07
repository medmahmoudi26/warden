#!/usr/bin/env bash
# HTTP desync checks: CL.0 smuggling through obfuscated Content-Length lines, CRLF splitting through Unicode folds in the path (Temporal worker, DesyncScan workflow).
#
# Warden runs this in each worker slot's terminal on every fleet machine, from
# /opt/warden/modules/desync, with WARDEN_* and TEMPORAL_ADDRESS set.
# Pure Python on raw sockets: nothing to install beyond uv, which the machine has.
# `entrypoint.sh --install-only` has nothing to install and exits.
set -euo pipefail

cd "$(dirname "$0")"
[[ "${1:-}" == "--install-only" ]] && exit 0

exec uv run --quiet --script worker.py
