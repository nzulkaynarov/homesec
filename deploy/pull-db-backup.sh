#!/usr/bin/env bash
# Pulls a verified snapshot of the HomeSec database from the Pi to this PC.
# Run by the user timer in deploy/pc; setup is in docs/05-operations.md.
set -euo pipefail
umask 077

PI_HOST=${HOMESEC_PI_HOST:-znz@192.168.88.2}
PI_BACKEND=${HOMESEC_PI_BACKEND:-/opt/homesec/backend}
BACKUP_DIR=${HOMESEC_BACKUP_DIR:-/data/backups/homesec}
KEEP=${HOMESEC_BACKUP_KEEP:-30}
HC_URL=${HOMESEC_BACKUP_HC_URL:-}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

ping_monitor() {
  [ -n "$HC_URL" ] || return 0
  curl -fsS -m 10 --retry 5 -o /dev/null "$HC_URL$1" \
    || echo "warning: monitoring ping failed" >&2
}

partial=""
on_exit() {
  local status=$?
  [ -z "$partial" ] || rm -f "$partial"
  if [ "$status" -ne 0 ]; then
    echo "error: backup failed with status $status" >&2
    ping_monitor /fail
  fi
}
trap on_exit EXIT

mkdir -p "$BACKUP_DIR"
partial=$(mktemp "$BACKUP_DIR/.partial.XXXXXX")

ssh -o BatchMode=yes -o ConnectTimeout=15 "$PI_HOST" python3 - "$PI_BACKEND" \
  < "$SCRIPT_DIR/pi_db_snapshot.py" > "$partial"

python3 -I - "$partial" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
result = connection.execute("PRAGMA integrity_check").fetchone()[0]
tables = connection.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
if result != "ok" or tables == 0:
    sys.exit(f"snapshot failed verification: {result}, {tables} tables")
PY

target="$BACKUP_DIR/homesec-$(date +%Y%m%d-%H%M%S).db"
mv "$partial" "$target"
partial=""

find "$BACKUP_DIR" -maxdepth 1 -name 'homesec-*.db' | sort | head -n "-$KEEP" | xargs -r rm --

echo "saved $target ($(stat -c %s "$target") bytes)"
ping_monitor ""
