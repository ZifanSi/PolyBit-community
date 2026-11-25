#!/bin/sh
# Replace the PolyBit database with a dump made by scripts/backup.sh, then audit it.
#
#   sh scripts/restore.sh backups/polybit-....dump --yes
#
# This wipes the current database. The bot is stopped while restoring.

set -eu

if [ $# -ne 2 ] || [ "$2" != "--yes" ]; then
  echo "usage: sh scripts/restore.sh FILE.dump --yes   (wipes the current database)" >&2
  exit 2
fi
[ -f "$1" ] || { echo "no such file: $1" >&2; exit 1; }
file=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
cd "$(dirname "$0")/.."

docker compose stop bot
docker compose exec -T db pg_restore -U polybit -d polybit --clean --if-exists --no-owner < "$file"
# Refuse to start on a ledger that doesn't add up.
docker compose run --rm bot polybit audit
docker compose start bot
echo "restored $file"
