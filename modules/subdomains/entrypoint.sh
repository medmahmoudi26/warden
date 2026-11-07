#!/usr/bin/env bash
# reconftw-style subdomain enumeration: subfinder, DNS resolution, brute force and permutations (Temporal worker, SubdomainEnum workflow).
#
# Warden runs this in each worker slot's terminal on every fleet machine, from
# /opt/warden/modules/subdomains, with WARDEN_* and TEMPORAL_ADDRESS set.
# `entrypoint.sh --install-only` installs the tools and wordlists and exits.
# config/provider-config.yaml (git-ignored) holds subfinder's API keys.
set -euo pipefail

SUBFINDER_VERSION="2.17.0"
SUBFINDER_SHA256="a39c3e5c7a26ef3c430915c4d6cbf7716a00f26769a75e9405aba796142eba37"
SUBFINDER_URL="${SUBFINDER_URL:-https://github.com/projectdiscovery/subfinder/releases/download/v${SUBFINDER_VERSION}/subfinder_${SUBFINDER_VERSION}_linux_amd64.zip}"
PUREDNS_VERSION="2.1.1"
PUREDNS_SHA256="9f78ae91f4f58c3c3185be650c25ba38d5ef80fb092436054d17f67008a93344"
PUREDNS_URL="https://github.com/d3mondev/puredns/releases/download/v${PUREDNS_VERSION}/puredns-Linux-amd64.tgz"
# massdns (puredns's resolver engine) is not packaged for Ubuntu 24.04: built from its v1.1.0 commit.
MASSDNS_COMMIT="ca8114164361f6a296399d8160bb07d55bdc4843"
# gotator's v1.1 tag is not valid Go module semver, so the commit is pinned (checked by Go's checksum database).
GOTATOR_COMMIT="85554801a28a7c3a4a405fbdbb3e675c2c7813f1"
REGULATOR_COMMIT="2371a0607de25f0a01a138905ccd841cc465bba5"
REGULATOR_DEPS=(dank==0.0.23 datrie==0.8.3 editdistance==0.8.1 tldextract==5.4.0 dnspython==2.9.2)
# Wordlists and trusted resolvers, as reconftw uses them (reconftw main at this commit).
RECONFTW="https://raw.githubusercontent.com/six2dez/reconftw/39252113a854bdac0b785f144b9fae1fa8ca35fc/data/wordlists"
N0KOVO="https://raw.githubusercontent.com/n0kovo/n0kovo_subdomains/7b922cd6d6d2cb190030cfe719269170d3143692"
TRUSTED_RESOLVERS="https://gist.githubusercontent.com/six2dez/ae9ed7e5c786461868abd3f2344401b6/raw/trusted_resolvers.txt"
PUBLIC_RESOLVERS="https://raw.githubusercontent.com/trickest/resolvers/main/resolvers.txt"

BIN_DIR="${WARDEN_BIN_DIR:-/usr/local/bin}"
DATA_DIR="${WARDEN_DATA_DIR:-/opt/warden/data/subdomains}"
CONNTRACK_MAX=262144
CONNTRACK_TIMEOUTS=(udp_timeout=10 tcp_timeout_time_wait=10 tcp_timeout_syn_sent=20)

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# download URL SHA256 DEST: fetch URL to DEST, refusing it unless its SHA-256 matches.
download() {
  curl -fsSL --retry 5 -o "$3.new" "$1"
  if [[ "$(sha256 "$3.new")" != "$2" ]]; then
    echo "[subdomains] checksum mismatch for $(basename "$3") from $1; not installing" >&2
    rm -f "$3.new"
    return 1
  fi
  mv -f "$3.new" "$3"
}

# install_release NAME URL SHA256: the binary NAME from a release archive (.zip or .tgz).
install_release() {
  local name="$1" url="$2" sha="$3" tmp
  if [[ -x "$BIN_DIR/$name" && "$(cat "$BIN_DIR/.$name.sha256" 2>/dev/null)" == "$sha" ]]; then
    return 0
  fi
  echo "[subdomains] installing $name"
  tmp="$(mktemp -d)"
  download "$url" "$sha" "$tmp/archive"
  case "$url" in
    *.zip) unzip -o -q "$tmp/archive" "$name" -d "$tmp" ;;
    *) tar -xzf "$tmp/archive" -C "$tmp" "$name" ;;
  esac
  mkdir -p "$BIN_DIR"
  install -m 0755 "$tmp/$name" "$BIN_DIR/$name"
  echo "$sha" > "$BIN_DIR/.$name.sha256"
  rm -rf "$tmp"
}

# install_wordlist NAME URL SHA256 [gz]: a pinned data file at $DATA_DIR/NAME (gunzipped if gz).
install_wordlist() {
  local name="$1" url="$2" sha="$3" dest="$DATA_DIR/$1"
  if [[ -s "$dest" && "$(cat "$DATA_DIR/.$name.sha256" 2>/dev/null)" == "$sha" ]]; then
    return 0
  fi
  echo "[subdomains] fetching $name"
  mkdir -p "$DATA_DIR"
  if [[ "${4:-}" == gz ]]; then
    download "$url" "$sha" "$dest.gz"
    gzip -dc "$dest.gz" > "$dest" && rm -f "$dest.gz"
  else
    download "$url" "$sha" "$dest"
  fi
  echo "$sha" > "$DATA_DIR/.$name.sha256"
}

install_apt_packages() {
  local missing=() p
  for p in golang-go build-essential git; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  [[ ${#missing[@]} -eq 0 ]] && return 0
  echo "[subdomains] apt: installing ${missing[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get -o DPkg::Lock::Timeout=600 update -q
  apt-get -o DPkg::Lock::Timeout=600 install -y -q --no-install-recommends "${missing[@]}"
}

install_massdns() {
  if [[ -x "$BIN_DIR/massdns" && "$(cat "$BIN_DIR/.massdns.commit" 2>/dev/null)" == "$MASSDNS_COMMIT" ]]; then
    return 0
  fi
  echo "[subdomains] building massdns"
  local tmp
  tmp="$(mktemp -d)"
  git -C "$tmp" init -q
  git -C "$tmp" fetch -q --depth 1 https://github.com/blechschmidt/massdns.git "$MASSDNS_COMMIT"
  git -C "$tmp" checkout -q FETCH_HEAD
  make -s -C "$tmp" PROJECT_FLAGS='-DMASSDNS_REVISION=\"v1.1.0\"'
  install -m 0755 "$tmp/bin/massdns" "$BIN_DIR/massdns"
  echo "$MASSDNS_COMMIT" > "$BIN_DIR/.massdns.commit"
  rm -rf "$tmp"
}

install_gotator() {
  if [[ -x "$BIN_DIR/gotator" && "$(cat "$BIN_DIR/.gotator.commit" 2>/dev/null)" == "$GOTATOR_COMMIT" ]]; then
    return 0
  fi
  echo "[subdomains] installing gotator"
  GOBIN="$BIN_DIR" GOTOOLCHAIN=local HOME="${HOME:-/root}" go install "github.com/Josue87/gotator@$GOTATOR_COMMIT"
  echo "$GOTATOR_COMMIT" > "$BIN_DIR/.gotator.commit"
}

install_regulator() {
  local dir="$DATA_DIR/regulator" pin="$REGULATOR_COMMIT ${REGULATOR_DEPS[*]}"
  if [[ -x "$dir/.venv/bin/python" && "$(cat "$dir/.pin" 2>/dev/null)" == "$pin" ]]; then
    return 0
  fi
  echo "[subdomains] installing regulator"
  rm -rf "$dir"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" fetch -q --depth 1 https://github.com/cramppet/regulator.git "$REGULATOR_COMMIT"
  git -C "$dir" checkout -q FETCH_HEAD
  mkdir -p "$dir/logs"
  uv venv -q --python 3.12 --python-preference only-managed "$dir/.venv"
  # dank's setup.py imports pybind11, so dank is built against the venv's own copy.
  uv pip install -q --python "$dir/.venv/bin/python" pybind11==2.13.6 setuptools==80.9.0 wheel==0.45.1
  uv pip install -q --python "$dir/.venv/bin/python" --no-build-isolation-package dank "${REGULATOR_DEPS[@]}"
  echo "$pin" > "$dir/.pin"
}

refresh_public_resolvers() {
  # trickest's list is re-validated daily, so it is fetched fresh (at most once a day), not pinned;
  # puredns checks every answer against the trusted resolvers.
  local dest="$DATA_DIR/resolvers.txt" n
  if [[ -s "$dest" && -z "$(find "$dest" -mmin +1440)" ]]; then
    return 0
  fi
  echo "[subdomains] fetching public resolvers"
  curl -fsSL --retry 5 -o "$dest.new" "$PUBLIC_RESOLVERS"
  n="$(grep -cE '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' "$dest.new" || true)"
  if (( n < 1000 )); then
    echo "[subdomains] public resolvers list has only $n entries; not using it" >&2
    rm -f "$dest.new"
    [[ -s "$dest" ]]
    return
  fi
  mv -f "$dest.new" "$dest"
}

# massdns sends thousands of DNS queries a second and retries truncated answers over
# TCP (hundreds of connections a second: some public resolvers truncate everything).
# Every flow holds a conntrack entry (Tailscale's firewall rules load conntrack):
# closed TCP connections and unanswered SYNs for 120 s each by default. The default
# table (65,536 entries) then fills, and the kernel drops new packets: the worker's
# own lookups and Temporal heartbeats fail, and the brute force slows down.
# A larger table and shorter timeouts; a higher limit or shorter timeout already set
# is kept.
tune_conntrack() {
  local dir=/proc/sys/net/netfilter setting file value
  [[ -w "$dir/nf_conntrack_max" ]] || return 0
  if (( $(cat "$dir/nf_conntrack_max") < CONNTRACK_MAX )); then
    echo "$CONNTRACK_MAX" > "$dir/nf_conntrack_max"
  fi
  for setting in "${CONNTRACK_TIMEOUTS[@]}"; do
    file="$dir/nf_conntrack_${setting%=*}" value="${setting#*=}"
    if (( $(cat "$file") > value )); then
      echo "$value" > "$file"
    fi
  done
}

install_all() {
  tune_conntrack
  install_release subfinder "$SUBFINDER_URL" "$SUBFINDER_SHA256"
  install_release puredns "$PUREDNS_URL" "$PUREDNS_SHA256"
  install_apt_packages
  install_massdns
  install_gotator
  install_regulator
  install_wordlist subdomains.txt "$RECONFTW/subdomains.txt.gz" 3e0f56063409ad1461aa70e4719491f0e079a698bffe3a125622f5f92418284f gz
  install_wordlist permutations_list.txt "$RECONFTW/permutations_list.txt" ca9d00fc013d79e29bc4e3234f29717ac27a80cce6ea5bdc566d8a4f1a848906
  install_wordlist permutations_list_short.txt "$RECONFTW/permutations_list_short.txt" f53e58f531a9f9e9e3aa2ef4e2fd539f05777ca46a46d8cf40f0b60100cf6e85
  install_wordlist n0kovo_subdomains_huge.txt "$N0KOVO/n0kovo_subdomains_huge.txt" f0d5d80a9043ba57e82d238e170a68f5014f568d82f03b1425df6752670a47d6
  install_wordlist trusted_resolvers.txt "$TRUSTED_RESOLVERS" af041daf9ec8b86ec96ebaf90a88dbd4508dd670e259475f3ba9c41347946bf0
  refresh_public_resolvers
}

cd "$(dirname "$0")"
if command -v flock >/dev/null; then
  # Slots on one machine start together; install once.
  exec 9>/tmp/warden-subdomains-install.lock
  flock 9
  install_all
  flock -u 9
else
  install_all
fi
[[ "${1:-}" == "--install-only" ]] && exit 0

export PATH="$BIN_DIR:$PATH" WARDEN_DATA_DIR="$DATA_DIR"
exec uv run --quiet --script worker.py
