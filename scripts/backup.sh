#!/bin/sh
# Dump the PolyBit database from the docker compose stack into backups/.
#
#   sh scripts/backup.sh        -> backups/polybit-YYYYmmdd-HHMMSS.dump
#
# A dump holds every balance and transaction: keep it private. backups/ and *.dump
# are gitignored and blocked by the hooks. Copy dumps off the server regularly.

set -eu
cd "$(dirname "$0")/.."

mkdir -p backups
umask 077
out="backups/polybit-$(date -u +%Y%m%d-%H%M%S).dump"
trap 'rm -f "$out.partial"' EXIT

docker compose exec -T db pg_dump -U polybit -d polybit --format=custom > "$out.partial"
mv "$out.partial" "$out"
echo "saved $out ($(wc -c < "$out") bytes)"
