#!/usr/bin/env bash
# Requires bash; run as ./openbao-pi-migrate-pebble.sh, not sh.
if [ -z "${BASH_VERSION:-}" ]; then echo "run with bash, not sh" >&2; exit 1; fi
# Migrate the OpenBao Pi from the removed "file" storage backend to pebbledb.
#
# Run as root on the Pi. This script never reads or prints unseal keys or
# TLS private keys. The unit's ExecStartPost helper unseals with Shamir
# shares from /etc/openbao/unseal.key after the service starts.
#
# Paths match roles/openbao-pi and hosts/group_vars/openbao.yaml.
#
# Official sources:
#   https://openbao.org/docs/guides/migrations/k8s-pebbledb/
#   https://openbao.org/docs/commands/operator/migrate/
#   https://openbao.org/docs/configuration/storage/pebbledb/
#   https://openbao.org/community/release-notes/2-7-0/
#
# Usage:
#   ./openbao-pi-migrate-pebble.sh [--yes]
#   ./openbao-pi-migrate-pebble.sh --rollback /var/backups/openbao/<backup>.tar.gz [--yes]

set -euo pipefail

OPENBAO_USER="openbao"
OPENBAO_GROUP="openbao"
CONFIG_DIR="/etc/openbao"
CONFIG_FILE="${CONFIG_DIR}/openbao.hcl"
DATA_DIR="/var/lib/openbao"
PEBBLE_DIR="/var/lib/openbao-pebbledb"  # sibling, not inside the file tree being migrated
TLS_CA="${CONFIG_DIR}/tls/ca.crt"
BAO_BIN="/usr/local/bin/bao"
SERVICE="openbao.service"
BACKUP_ROOT="/var/backups/openbao"
STATUS_ADDR="https://127.0.0.1:8200"
TARGET_VERSION="2.7.2"
MIGRATE_CONFIG="${CONFIG_DIR}/migrate-file-to-pebbledb.hcl"
CONFIG_BACKUP_SUFFIX=".pre-pebble"

usage() {
  cat <<'EOF'
Usage:
  openbao-pi-migrate-pebble.sh [--yes]
  openbao-pi-migrate-pebble.sh --rollback <backup.tar.gz> [--yes]

Migrates OpenBao file storage at /var/lib/openbao to pebbledb at
/var/lib/openbao-pebbledb. The OpenBao server must be stopped during
the copy. A 2.7.x bao binary is required: 2.6.x cannot write pebbledb,
and 2.7.x still allows "file" as a migrate source until 2.8.0.
EOF
}

log() { printf '%s %s\n' "$(date -Is)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

require_root() {
  if [[ ${EUID} -ne 0 ]]; then
    die "run as root on the Pi"
  fi
}

require_tools() {
  local t
  for t in tar sha256sum awk sed systemctl install stat df du date mktemp; do
    command -v "$t" >/dev/null 2>&1 || die "missing required command: $t"
  done
}

confirm() {
  local prompt="$1"
  if [[ ${ASSUME_YES} -eq 1 ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    die "stdin is not a tty; re-run with --yes"
  fi
  local reply
  read -r -p "${prompt} [yes/no] " reply
  [[ ${reply} == yes ]] || die "aborted"
}

version_from_bao() {
  local bin="$1"
  local raw
  raw="$("$bin" version 2>/dev/null || true)"
  awk '
    match($0, /[0-9]+\.[0-9]+\.[0-9]+/) {
      print substr($0, RSTART, RLENGTH)
      exit
    }
  ' <<<"${raw}"
}

version_ge() {
  # Returns 0 if $1 >= $2 (dotted numeric triples).
  local IFS=.
  # shellcheck disable=SC2086
  set -- $1 $2
  local a=${1:-0} b=${2:-0} c=${3:-0} d=${4:-0} e=${5:-0} f=${6:-0}
  if (( a > d )); then return 0; fi
  if (( a < d )); then return 1; fi
  if (( b > e )); then return 0; fi
  if (( b < e )); then return 1; fi
  if (( c >= f )); then return 0; fi
  return 1
}

detect_arch() {
  case "$(uname -m)" in
    aarch64|arm64) printf 'arm64\n' ;;
    x86_64|amd64) printf 'amd64\n' ;;
    *) die "unsupported architecture: $(uname -m)" ;;
  esac
}

available_bytes() {
  df -B1 --output=avail "$1" | awk 'NR==2 {gsub(/[[:space:]]/,""); print}'
}

used_percent() {
  df --output=pcent "$1" | awk 'NR==2 {gsub(/[%[:space:]]/,""); print}'
}

dir_bytes() {
  du -sb "$1" | awk '{print $1}'
}

storage_stanza() {
  local cfg="$1"
  [[ -f $cfg ]] || return 1
  awk '
    $0 ~ /^[[:space:]]*storage[[:space:]]+"/ {
      if (match($0, /storage[[:space:]]+"[^"]+"/)) {
        line = substr($0, RSTART, RLENGTH)
        sub(/^storage[[:space:]]+"/, "", line)
        sub(/"$/, "", line)
        print line
        exit
      }
    }
  ' "$cfg"
}

stop_openbao() {
  # Always stop: this also cancels a pending Restart=on-failure job, so
  # systemd cannot relaunch the old server in the middle of the migrate.
  log "stopping ${SERVICE} (and any pending restart)"
  systemctl stop "$SERVICE" || true
  local _try
  for _try in $(seq 1 30); do
    if ! systemctl is-active --quiet "$SERVICE"; then
      return 0
    fi
    sleep 1
  done
  die "${SERVICE} did not stop"
}

start_openbao() {
  log "starting ${SERVICE} (ExecStartPost unseals with Shamir; this script does not read the keyfile)"
  systemctl start "$SERVICE"
}

wait_unsealed() {
  local tries="${1:-60}"
  local status_out sealed storage_type version
  export BAO_ADDR="${STATUS_ADDR}"
  if [[ -r $TLS_CA ]]; then
    export BAO_CACERT="${TLS_CA}"
  fi
  local _try
  for _try in $(seq 1 "${tries}"); do
    status_out="$("$BAO_BIN" status -format=json 2>/dev/null || true)"
    if [[ -n $status_out ]]; then
      sealed="$(printf '%s\n' "$status_out" | awk -F: '/"sealed"/ {gsub(/[ ,]/,"",$2); print $2; exit}')"
      storage_type="$(printf '%s\n' "$status_out" | awk -F: '/"storage_type"/ {gsub(/[ ",]/,"",$2); print $2; exit}')"
      version="$(printf '%s\n' "$status_out" | awk -F: '/"version"/ {gsub(/[ ",]/,"",$2); print $2; exit}')"
      if [[ $sealed == false ]]; then
        log "openbao is unsealed: version=${version:-unknown} storage_type=${storage_type:-unknown}"
        "$BAO_BIN" status || true
        if [[ -n $storage_type && $storage_type != pebbledb ]]; then
          die "expected storage_type pebbledb, got ${storage_type}"
        fi
        return 0
      fi
    fi
    sleep 1
  done
  log "timed out waiting for unseal; last bao status:"
  "$BAO_BIN" status || true
  die "OpenBao did not unseal. Check journalctl -u ${SERVICE} and the ExecStartPost unseal helper. The keyfile was not read by this script."
}

write_migrate_config() {
  cat > "$MIGRATE_CONFIG" <<EOF
storage_source "file" {
  path = "${DATA_DIR}"
}

storage_destination "pebbledb" {
  path = "${PEBBLE_DIR}"
}
EOF
  chown "${OPENBAO_USER}:${OPENBAO_GROUP}" "$MIGRATE_CONFIG"
  chmod 0640 "$MIGRATE_CONFIG"
}

swap_config_to_pebble() {
  local tmp
  tmp="$(mktemp)"
  if [[ ! -f ${CONFIG_FILE}${CONFIG_BACKUP_SUFFIX} ]]; then
    cp -a "$CONFIG_FILE" "${CONFIG_FILE}${CONFIG_BACKUP_SUFFIX}"
    chmod 0640 "${CONFIG_FILE}${CONFIG_BACKUP_SUFFIX}"
  fi
  awk -v pebble_dir="$PEBBLE_DIR" '
    BEGIN { in_storage = 0 }
    /^[[:space:]]*storage[[:space:]]+"file"[[:space:]]*\{/ {
      print "storage \"pebbledb\" {"
      print "  path = \"" pebble_dir "\""
      print "}"
      in_storage = 1
      next
    }
    in_storage {
      if ($0 ~ /^[[:space:]]*}/) {
        in_storage = 0
      }
      next
    }
    { print }
  ' "$CONFIG_FILE" > "$tmp"
  if ! grep -q 'storage "pebbledb"' "$tmp"; then
    rm -f "$tmp"
    die "failed to rewrite storage stanza in ${CONFIG_FILE}"
  fi
  if grep -q 'storage "file"' "$tmp"; then
    rm -f "$tmp"
    die "file storage stanza still present after rewrite"
  fi
  install -o "$OPENBAO_USER" -g "$OPENBAO_GROUP" -m 0640 "$tmp" "$CONFIG_FILE"
  rm -f "$tmp"
}

make_backup() {
  local stamp="$1"
  local archive="${BACKUP_ROOT}/openbao-file-${stamp}.tar.gz"
  local meta="${BACKUP_ROOT}/openbao-file-${stamp}.meta.txt"
  mkdir -p "$BACKUP_ROOT"
  chmod 0700 "$BACKUP_ROOT"
  umask 077
  log "writing backup ${archive}"
  {
    printf 'created=%s\n' "$(date -Is)"
    printf 'hostname=%s\n' "$(hostname -f 2>/dev/null || hostname)"
    printf 'bao_version=%s\n' "$(version_from_bao "$BAO_BIN" || printf 'unknown')"
    printf 'data_dir=%s\n' "$DATA_DIR"
    printf 'config_file=%s\n' "$CONFIG_FILE"
    printf 'bao_bin=%s\n' "$BAO_BIN"
  } > "$meta"
  chmod 0600 "$meta"
  # Config is the HCL only. Unseal keys and TLS material stay in place and
  # are not packed here.
  tar -C / --exclude="${PEBBLE_DIR#/}" --exclude="${DATA_DIR#/}/pebbledb" \
    -czf "$archive" \
    "${DATA_DIR#/}" \
    "${CONFIG_FILE#/}" \
    "${BAO_BIN#/}"
  sha256sum "$archive" | tee "${archive}.sha256" >/dev/null
  chmod 0600 "$archive" "${archive}.sha256"
  sha256sum -c "${archive}.sha256" >&2
  tar -tzf "$archive" >/dev/null
  local listed
  listed="$(tar -tzf "$archive")"
  printf '%s\n' "$listed" | grep -q "${CONFIG_FILE#/}" || die "backup is missing ${CONFIG_FILE}"
  printf '%s\n' "$listed" | grep -q "${DATA_DIR#/}" || die "backup is missing ${DATA_DIR}"
  printf '%s\n' "$listed" | grep -q "${BAO_BIN#/}" || die "backup is missing ${BAO_BIN}"
  log "backup verified: $(sha256sum "$archive")"
  log "backup listing written via tar -t; file count=$(printf '%s\n' "$listed" | wc -l)"
  printf '%s\n' "$archive"
}

obtain_migrate_binary() {
  local installed_ver workdir tarball url arch
  if [[ -n ${BAO_MIGRATE_BIN:-} ]]; then
    [[ -x $BAO_MIGRATE_BIN ]] || die "BAO_MIGRATE_BIN is not executable: ${BAO_MIGRATE_BIN}"
    printf '%s\n' "$BAO_MIGRATE_BIN"
    return 0
  fi
  if [[ -x $BAO_BIN ]]; then
    installed_ver="$(version_from_bao "$BAO_BIN")"
    if [[ -n $installed_ver ]] && version_ge "$installed_ver" "2.7.0" && ! version_ge "$installed_ver" "2.8.0"; then
      log "using installed ${BAO_BIN} (${installed_ver}) for migrate"
      printf '%s\n' "$BAO_BIN"
      return 0
    fi
  fi
  command -v curl >/dev/null 2>&1 || die "curl is required to download OpenBao ${TARGET_VERSION}"
  arch="$(detect_arch)"
  tarball="openbao_${TARGET_VERSION}_linux_${arch}.tar.gz"
  url="https://github.com/openbao/openbao/releases/download/v${TARGET_VERSION}/${tarball}"
  workdir="$(mktemp -d /var/tmp/openbao-migrate-bin.XXXXXX)"
  chmod 0700 "$workdir"
  log "downloading ${url}"
  curl -fsSL -o "${workdir}/${tarball}" "$url"
  curl -fsSL -o "${workdir}/checksums.txt" \
    "https://github.com/openbao/openbao/releases/download/v${TARGET_VERSION}/checksums.txt"
  (
    cd "$workdir"
    grep -E "[[:space:]]${tarball}\$" checksums.txt > checksum.line
    [[ -s checksum.line ]] || die "checksums.txt has no entry for ${tarball}"
    sha256sum -c checksum.line >&2
  )
  tar -C "$workdir" -xzf "${workdir}/${tarball}" bao
  chmod 0755 "${workdir}/bao"
  chmod 0755 "$workdir"  # let the openbao user reach the binary
  local got
  got="$(version_from_bao "${workdir}/bao")"
  [[ $got == "$TARGET_VERSION" ]] || die "downloaded bao reports version ${got:-unknown}, expected ${TARGET_VERSION}"
  log "downloaded bao ${got} for migrate"
  printf '%s\n' "${workdir}/bao"
}

install_target_binary() {
  local src="$1"
  local ver
  ver="$(version_from_bao "$src")"
  local installed=""
  if [[ -x $BAO_BIN ]]; then
    installed="$(version_from_bao "$BAO_BIN")"
  fi
  if [[ $installed == "$ver" ]]; then
    log "${BAO_BIN} is already ${ver}"
    return 0
  fi
  log "installing bao ${ver} to ${BAO_BIN} (previous ${installed:-none} is in the backup tarball)"
  install -o root -g root -m 0755 "$src" "$BAO_BIN"
}

fix_ownership() {
  chown -R "${OPENBAO_USER}:${OPENBAO_GROUP}" "$DATA_DIR" "$PEBBLE_DIR"
  chmod 0750 "$DATA_DIR" "$PEBBLE_DIR"
  chown "${OPENBAO_USER}:${OPENBAO_GROUP}" "$CONFIG_FILE"
  chmod 0640 "$CONFIG_FILE"
}

print_rollback() {
  local archive="$1"
  cat <<EOF

Migration finished. File data was left in ${DATA_DIR} (operator migrate
does not delete the source). After you are satisfied, take a new backup
and then remove the old file-backend tree, keeping ${PEBBLE_DIR}.

Rollback (restores the pre-migration binary, HCL, and file data):
  $0 --rollback ${archive}

Do not start a 2.7.x binary with storage "file"; that backend was removed
from the server in 2.7.0. The backup tarball includes the previous bao
binary for this reason.
EOF
}

preflight_migrate() {
  require_root
  require_tools
  [[ -f $CONFIG_FILE ]] || die "missing config ${CONFIG_FILE}"
  [[ -d $DATA_DIR ]] || die "missing data dir ${DATA_DIR}"
  [[ -x $BAO_BIN ]] || die "missing ${BAO_BIN}"
  getent passwd "$OPENBAO_USER" >/dev/null || die "user ${OPENBAO_USER} does not exist"
  getent group "$OPENBAO_GROUP" >/dev/null || die "group ${OPENBAO_GROUP} does not exist"

  local stanza
  stanza="$(storage_stanza "$CONFIG_FILE" || true)"
  if [[ $stanza == pebbledb ]]; then
    die "config already uses storage \"pebbledb\"; nothing to migrate. Use --rollback to go back."
  fi
  if [[ $stanza != file ]]; then
    die "expected storage \"file\" in ${CONFIG_FILE}, found \"${stanza:-none}\""
  fi
  if [[ ! -d ${DATA_DIR}/core && ! -d ${DATA_DIR}/sys && ! -d ${DATA_DIR}/logical ]]; then
    die "${DATA_DIR} does not look like a file-backend tree (no core/, sys/, or logical/)"
  fi
  if [[ -e $PEBBLE_DIR ]] && find "$PEBBLE_DIR" -mindepth 1 -maxdepth 1 | grep -q .; then
    die "${PEBBLE_DIR} already has files; destination must be empty. Move it aside or use --rollback."
  fi

  local data_size avail pct need
  data_size="$(dir_bytes "$DATA_DIR")"
  avail="$(available_bytes "$DATA_DIR")"
  pct="$(used_percent "$DATA_DIR")"
  # One copy for the tarball (worst case uncompressed) plus one for pebbledb.
  need=$(( data_size * 2 + 64*1024*1024 ))
  log "data dir size=${data_size} bytes, avail=${avail} bytes, used=${pct}%"
  if (( avail < need )); then
    die "not enough free space under $(dirname "$DATA_DIR"): need about ${need} bytes for a backup plus the pebbledb copy"
  fi
  # Official guide recommends 60% free while both formats exist.
  if (( pct > 40 )); then
    log "WARNING: official migrate guide recommends at least 60% free space (this filesystem is ${pct}% used). Continuing because available bytes exceed twice the data size."
  fi

  local inst
  inst="$(version_from_bao "$BAO_BIN")"
  log "installed bao version=${inst:-unknown}"
}

do_migrate() {
  preflight_migrate
  confirm "Stop OpenBao, back up ${DATA_DIR} and ${CONFIG_FILE}, migrate to pebbledb, and start 2.7.x?"

  local stamp archive migrate_bin
  stamp="$(date +%Y%m%dT%H%M%S)"
  migrate_bin="$(obtain_migrate_binary)"
  local migrate_ver
  migrate_ver="$(version_from_bao "$migrate_bin")"
  if [[ -z $migrate_ver ]] || ! version_ge "$migrate_ver" "2.7.0"; then
    die "migrate binary must be OpenBao 2.7.x (got ${migrate_ver:-unknown}); pebbledb arrived in 2.7.0"
  fi
  if version_ge "$migrate_ver" "2.8.0"; then
    die "OpenBao 2.8+ removes file support from operator migrate. Use a 2.7.x binary (got ${migrate_ver})"
  fi

  stop_openbao
  archive="$(make_backup "$stamp")"

  mkdir -p "$PEBBLE_DIR"
  chown "${OPENBAO_USER}:${OPENBAO_GROUP}" "$PEBBLE_DIR"
  chmod 0750 "$PEBBLE_DIR"
  write_migrate_config

  log "running: ${migrate_bin} operator migrate -config ${MIGRATE_CONFIG}"
  if command -v runuser >/dev/null 2>&1; then
    if ! runuser -u "$OPENBAO_USER" -- "$migrate_bin" operator migrate -config "$MIGRATE_CONFIG"; then
      _migrate_failed=1
    else
      _migrate_failed=0
    fi
  else
    if ! sudo -u "$OPENBAO_USER" -- "$migrate_bin" operator migrate -config "$MIGRATE_CONFIG"; then
      _migrate_failed=1
    else
      _migrate_failed=0
    fi
  fi
  if [[ ${_migrate_failed} -ne 0 ]]; then
    log "migrate failed. Destination ${PEBBLE_DIR} may be incomplete."
    log "Source file data was not deleted. Config was not swapped."
    log "If a migrate lock remains: ${migrate_bin} operator migrate -config ${MIGRATE_CONFIG} -reset"
    die "bao operator migrate failed"
  fi

  swap_config_to_pebble
  install_target_binary "$migrate_bin"
  fix_ownership
  start_openbao
  wait_unsealed 60
  print_rollback "$archive"
}

do_rollback() {
  local archive="$1"
  require_root
  require_tools
  [[ -f $archive ]] || die "backup not found: ${archive}"
  if [[ -f ${archive}.sha256 ]]; then
    log "verifying ${archive}.sha256"
    sha256sum -c "${archive}.sha256"
  else
    log "WARNING: no sidecar checksum at ${archive}.sha256"
  fi
  tar -tzf "$archive" >/dev/null
  confirm "Stop OpenBao and restore file storage from ${archive}?"

  stop_openbao
  local safety
  safety="${BACKUP_ROOT}/openbao-pre-rollback-$(date +%Y%m%dT%H%M%S).tar.gz"
  mkdir -p "$BACKUP_ROOT"
  chmod 0700 "$BACKUP_ROOT"
  umask 077
  log "saving current tree to ${safety} before rollback"
  tar -C / -czf "$safety" \
    --exclude="${PEBBLE_DIR#/}" \
    "${DATA_DIR#/}" \
    "${CONFIG_FILE#/}" \
    "${BAO_BIN#/}" \
    || true
  if [[ -f $safety ]]; then
    sha256sum "$safety" | tee "${safety}.sha256" >/dev/null
    chmod 0600 "$safety" "${safety}.sha256"
  fi

  if [[ -d $PEBBLE_DIR ]]; then
    local aside
    aside="${PEBBLE_DIR}.aside.$(date +%Y%m%dT%H%M%S)"
    log "moving ${PEBBLE_DIR} aside to ${aside}"
    mv "$PEBBLE_DIR" "$aside"
  fi

  log "extracting ${archive} to /"
  tar -C / -xzf "$archive"
  fix_ownership
  if [[ -f $BAO_BIN ]]; then
    chown root:root "$BAO_BIN"
    chmod 0755 "$BAO_BIN"
  fi
  local restored_ver stanza
  restored_ver="$(version_from_bao "$BAO_BIN")"
  stanza="$(storage_stanza "$CONFIG_FILE" || true)"
  log "restored bao ${restored_ver:-unknown}, storage=${stanza:-unknown}"
  if [[ $stanza != file ]]; then
    die "restored config does not use storage \"file\""
  fi
  if [[ -n $restored_ver ]] && version_ge "$restored_ver" "2.7.0"; then
    die "restored binary is ${restored_ver}, which cannot start with storage \"file\". Restore a pre-2.7 bao binary from the backup."
  fi
  start_openbao
  export BAO_ADDR="${STATUS_ADDR}"
  if [[ -r $TLS_CA ]]; then
    export BAO_CACERT="${TLS_CA}"
  fi
  local status_out sealed
  local _try
  for _try in $(seq 1 60); do
    status_out="$("$BAO_BIN" status -format=json 2>/dev/null || true)"
    sealed="$(printf '%s\n' "$status_out" | awk -F: '/"sealed"/ {gsub(/[ ,]/,"",$2); print $2; exit}')"
    if [[ $sealed == false ]]; then
      log "rollback complete; OpenBao is unsealed on the file backend"
      "$BAO_BIN" status || true
      return 0
    fi
    sleep 1
  done
  log "rollback extracted, but unseal was not confirmed:"
  "$BAO_BIN" status || true
  die "restored OpenBao did not unseal"
}

ASSUME_YES=0
MODE="migrate"
ROLLBACK_ARCHIVE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --yes|-y)
      ASSUME_YES=1
      shift
      ;;
    --rollback)
      MODE="rollback"
      shift
      [[ $# -gt 0 ]] || die "--rollback requires a backup tarball path"
      ROLLBACK_ARCHIVE="$1"
      shift
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

if [[ $MODE == rollback ]]; then
  do_rollback "$ROLLBACK_ARCHIVE"
else
  do_migrate
fi
