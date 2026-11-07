#!/usr/bin/env bash
# Vulnerability scanning with nuclei: all default templates, every severity (Temporal worker, NucleiScan workflow).
#
# Warden runs this in each worker slot's terminal on every fleet machine, from
# /opt/warden/modules/nuclei, with WARDEN_* and TEMPORAL_ADDRESS set.
# `entrypoint.sh --install-only` installs nuclei and its templates and exits.
set -euo pipefail

NUCLEI_VERSION="3.11.1"
NUCLEI_SHA256="ea63d4ae232808cd7c6bc00d0142428e231fab59dae01042246097d195835ab6"
NUCLEI_URL="${NUCLEI_URL:-https://github.com/projectdiscovery/nuclei/releases/download/v${NUCLEI_VERSION}/nuclei_${NUCLEI_VERSION}_linux_amd64.zip}"
BIN_DIR="${WARDEN_BIN_DIR:-/usr/local/bin}"
DATA_DIR="${WARDEN_DATA_DIR:-/opt/warden/data/nuclei}"

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

install_nuclei() {
  if [[ -x "$BIN_DIR/nuclei" && "$(cat "$BIN_DIR/.nuclei.sha256" 2>/dev/null)" == "$NUCLEI_SHA256" ]]; then
    return 0
  fi
  echo "[nuclei] installing nuclei v${NUCLEI_VERSION}"
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL --retry 5 -o "$tmp/nuclei.zip" "$NUCLEI_URL"
  if [[ "$(sha256 "$tmp/nuclei.zip")" != "$NUCLEI_SHA256" ]]; then
    echo "[nuclei] checksum mismatch for nuclei v${NUCLEI_VERSION}; not installing" >&2
    rm -rf "$tmp"
    return 1
  fi
  unzip -o -q "$tmp/nuclei.zip" nuclei -d "$tmp"
  mkdir -p "$BIN_DIR"
  install -m 0755 "$tmp/nuclei" "$BIN_DIR/nuclei"
  echo "$NUCLEI_SHA256" > "$BIN_DIR/.nuclei.sha256"
  rm -rf "$tmp"
}

update_templates() {
  # The latest templates (fresh checks are their point), at most once a day.
  # The version is recorded for each run's result.
  local stamp="$DATA_DIR/templates-version" version
  if [[ -s "$stamp" && -z "$(find "$stamp" -mmin +1440)" ]]; then
    return 0
  fi
  echo "[nuclei] updating templates"
  "$BIN_DIR/nuclei" -update-templates -nc  # not -duc: it disables this update too
  version="$("$BIN_DIR/nuclei" -templates-version -duc -nc 2>&1 | grep -oE 'templates version: +v[0-9.]+' | grep -oE 'v[0-9.]+$' || true)"
  if [[ -z "$version" ]]; then
    echo "[nuclei] templates are not installed" >&2
    return 1
  fi
  mkdir -p "$DATA_DIR"
  echo "$version" > "$stamp"
  echo "[nuclei] templates $version"
}

cd "$(dirname "$0")"
if command -v flock >/dev/null; then
  # Slots on one machine start together; install once.
  exec 9>/tmp/warden-nuclei-install.lock
  flock 9
  install_nuclei
  update_templates
  flock -u 9
else
  install_nuclei
  update_templates
fi
[[ "${1:-}" == "--install-only" ]] && exit 0

export NUCLEI_BIN="$BIN_DIR/nuclei" WARDEN_DATA_DIR="$DATA_DIR"
exec uv run --quiet --script worker.py
