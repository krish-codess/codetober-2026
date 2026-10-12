#!/usr/bin/env bash
# Nightly backup, run on the VM from cron:   15 2 * * *  ~/ghostfleet/INGEST/ops/backup.sh /var/backups/ghostfleet
#
# Two things are worth keeping: the database, and the raw archive (from which the positions in the
# database can be rebuilt by replay). Kafka is a buffer and is not backed up.
set -euo pipefail

dest=${1:?usage: backup.sh <backup-dir> [days-to-keep]}
keep=${2:-14}
cd "$(dirname "$0")/.."
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$dest"

# Custom format: compressed, and restorable table by table with pg_restore.
docker compose exec -T postgis pg_dump -U ghost_owner -d ghost --format=custom > "$dest/ghost-$stamp.dump.partial"
mv "$dest/ghost-$stamp.dump.partial" "$dest/ghost-$stamp.dump"

# Closed hour files never change, so a plain copy of anything not already there is a complete incremental.
docker compose run --rm --no-deps -T --entrypoint tar collector -C /app/data -cf - raw | tar -C "$dest" -xf - --skip-old-files

# A backup that cannot be read is not a backup.
docker compose exec -T postgis pg_restore --list < "$dest/ghost-$stamp.dump" > /dev/null

find "$dest" -name 'ghost-*.dump' -mtime +"$keep" -delete
echo "backup ok: $dest/ghost-$stamp.dump ($(du -h "$dest/ghost-$stamp.dump" | cut -f1))"
