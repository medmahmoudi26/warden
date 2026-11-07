#!/usr/bin/env bash
# Sensitive-path discovery with ffuf and the leaky-paths wordlist, reconftw-style (Temporal worker, WebFuzz workflow).
#
# Warden runs this in each worker slot's terminal on every fleet machine, from
# /opt/warden/modules/ffuf, with WARDEN_* and TEMPORAL_ADDRESS set.
# `entrypoint.sh --install-only` installs ffuf and the wordlist and exits.
set -euo pipefail

FFUF_VERSION="2.3.0"
FFUF_SHA256="b2a3c725fcb9da175159682f54d6e9149f2905b00d84d155e7efc5d599975ceb"
FFUF_URL="${FFUF_URL:-https://github.com/ffuf/ffuf/releases/download/v${FFUF_VERSION}/ffuf_${FFUF_VERSION}_linux_amd64.tar.gz}"
LEAKY_PATHS_URL="https://raw.githubusercontent.com/ayoubfathi/leaky-paths/2210a2bfd9e2c826b5767e8e9c849618fdd71b87/leaky-paths.txt"
LEAKY_PATHS_SHA256="958e9e04cdf9de15868cc019b70c77d928570f50ce2733f79ed5859ca008cf90"
BIN_DIR="${WARDEN_BIN_DIR:-/usr/local/bin}"
DATA_DIR="${WARDEN_DATA_DIR:-/opt/warden/data/ffuf}"

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

install_ffuf() {
  if [[ -x "$BIN_DIR/ffuf" && "$(cat "$BIN_DIR/.ffuf.sha256" 2>/dev/null)" == "$FFUF_SHA256" ]]; then
    return 0
  fi
  echo "[ffuf] installing ffuf v${FFUF_VERSION}"
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL --retry 5 -o "$tmp/ffuf.tar.gz" "$FFUF_URL"
  if [[ "$(sha256 "$tmp/ffuf.tar.gz")" != "$FFUF_SHA256" ]]; then
    echo "[ffuf] checksum mismatch for ffuf v${FFUF_VERSION}; not installing" >&2
    rm -rf "$tmp"
    return 1
  fi
  tar -xzf "$tmp/ffuf.tar.gz" -C "$tmp" ffuf
  mkdir -p "$BIN_DIR"
  install -m 0755 "$tmp/ffuf" "$BIN_DIR/ffuf"
  echo "$FFUF_SHA256" > "$BIN_DIR/.ffuf.sha256"
  rm -rf "$tmp"
}

install_wordlist() {
  local dest="$DATA_DIR/leaky-paths.txt"
  if [[ -s "$dest" && "$(sha256 "$dest")" == "$LEAKY_PATHS_SHA256" ]]; then
    return 0
  fi
  echo "[ffuf] fetching leaky-paths"
  mkdir -p "$DATA_DIR"
  curl -fsSL --retry 5 -o "$dest.new" "$LEAKY_PATHS_URL"
  if [[ "$(sha256 "$dest.new")" != "$LEAKY_PATHS_SHA256" ]]; then
    echo "[ffuf] checksum mismatch for leaky-paths.txt; not installing" >&2
    rm -f "$dest.new"
    return 1
  fi
  mv -f "$dest.new" "$dest"
}

cd "$(dirname "$0")"
if command -v flock >/dev/null; then
  # Slots on one machine start together; install once.
  exec 9>/tmp/warden-ffuf-install.lock
  flock 9
  install_ffuf
  install_wordlist
  flock -u 9
else
  install_ffuf
  install_wordlist
fi
[[ "${1:-}" == "--install-only" ]] && exit 0

export FFUF_BIN="$BIN_DIR/ffuf" WARDEN_DATA_DIR="$DATA_DIR"
exec uv run --quiet --script worker.py
