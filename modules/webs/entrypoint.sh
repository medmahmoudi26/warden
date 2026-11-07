#!/usr/bin/env bash
# Web probing with httpx: status, length, type, title, server, technologies, CNAMEs, IPs, CDN, TLS (Temporal worker, WebProbe workflow).
#
# Warden runs this in each worker slot's terminal on every fleet machine, from
# /opt/warden/modules/webs, with WARDEN_* and TEMPORAL_ADDRESS set.
# `entrypoint.sh --install-only` installs httpx and exits.
set -euo pipefail

HTTPX_VERSION="1.12.0"
HTTPX_SHA256="9d8439e8b6c9aa7d1e2314817a392e00d5178da3af5652f7475f88868f418f76"
HTTPX_URL="${HTTPX_URL:-https://github.com/projectdiscovery/httpx/releases/download/v${HTTPX_VERSION}/httpx_${HTTPX_VERSION}_linux_amd64.zip}"
BIN_DIR="${WARDEN_BIN_DIR:-/usr/local/bin}"

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

install_httpx() {
  if [[ -x "$BIN_DIR/httpx" && "$(cat "$BIN_DIR/.httpx.sha256" 2>/dev/null)" == "$HTTPX_SHA256" ]]; then
    return 0
  fi
  echo "[webs] installing httpx v${HTTPX_VERSION}"
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL --retry 5 -o "$tmp/httpx.zip" "$HTTPX_URL"
  if [[ "$(sha256 "$tmp/httpx.zip")" != "$HTTPX_SHA256" ]]; then
    echo "[webs] checksum mismatch for httpx v${HTTPX_VERSION}; not installing" >&2
    rm -rf "$tmp"
    return 1
  fi
  unzip -o -q "$tmp/httpx.zip" httpx -d "$tmp"
  mkdir -p "$BIN_DIR"
  install -m 0755 "$tmp/httpx" "$BIN_DIR/httpx"
  echo "$HTTPX_SHA256" > "$BIN_DIR/.httpx.sha256"
  rm -rf "$tmp"
}

cd "$(dirname "$0")"
if command -v flock >/dev/null; then
  # Slots on one machine start together; install once.
  exec 9>/tmp/warden-webs-install.lock
  flock 9
  install_httpx
  flock -u 9
else
  install_httpx
fi
[[ "${1:-}" == "--install-only" ]] && exit 0

export HTTPX_BIN="$BIN_DIR/httpx"
exec uv run --quiet --script worker.py
