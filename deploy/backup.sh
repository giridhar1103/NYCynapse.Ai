#!/bin/bash
# Nightly backup of the app database (conversations, ratings, access requests, alerts, the
# catalog), compressed, kept for 14 days. Restore with:
#   pg_restore --clean --if-exists -d "$NYC_APP_PG_DSN" /root/backups/nycynapse/<file>.dump
set -euo pipefail
DIR=/root/backups/nycynapse
mkdir -p "$DIR"
chmod 700 "$DIR"
out="$DIR/nycynapse-$(date +%Y%m%d-%H%M).dump"
pg_dump --format=custom --compress=6 --no-owner --file="$out.part" "$NYC_APP_PG_DSN"
mv "$out.part" "$out"
chmod 600 "$out"
find "$DIR" -name 'nycynapse-*.dump' -mtime +14 -delete
echo "backup written: $out ($(du -h "$out" | cut -f1))"
