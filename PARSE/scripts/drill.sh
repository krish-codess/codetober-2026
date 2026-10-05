#!/usr/bin/env bash
# Operations drill: deploy a new image tag, roll back, roll forward, roll a model back, and
# prove a backup restores. Non-destructive: the restore goes into a scratch database.
# usage: scripts/drill.sh <old_tag> <new_tag>      (both image tags must already be built)
set -euo pipefail
OLD="${1:?old image tag}"; NEW="${2:?new image tag}"
set -a; . ./.env; set +a
BASE="http://127.0.0.1:${WEB_PORT:-8141}"
DB="$(docker compose ps -q db)"
sql() { docker exec -i "$DB" psql -U "$POSTGRES_USER" -d "${2:-$POSTGRES_DB}" -Atc "$1" < /dev/null; }
image_of() { docker inspect --format '{{.Config.Image}}' "$(docker compose ps -q "$1")"; }
PY=""; for c in python3 python py; do if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi; done
health() { curl -fsS --max-time 20 "$BASE/api/v1/health" | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d['status'], 'model', d['checks']['model'].get('model_version'), 'schema', d['checks']['database'].get('schema'))"; }
step() { echo; echo "=== $* ($(date -u +%H:%M:%SZ))"; }

step "0. starting point"
echo "api image: $(image_of api)"; health

step "1. deploy $NEW"
IMAGE_TAG="$NEW" docker compose up -d --no-build --wait api worker web 2>&1 | tail -4
echo "api image: $(image_of api)   worker image: $(image_of worker)   web image: $(image_of web)"
health
bash scripts/smoke.sh "$BASE" | tail -1

step "2. roll back to $OLD"
IMAGE_TAG="$OLD" docker compose up -d --no-build --wait api worker web 2>&1 | tail -4
echo "api image: $(image_of api)   worker image: $(image_of worker)   web image: $(image_of web)"
health
bash scripts/smoke.sh "$BASE" | tail -1

step "3. roll forward to $NEW"
IMAGE_TAG="$NEW" docker compose up -d --no-build --wait api worker web 2>&1 | tail -4
echo "api image: $(image_of api)"; health

step "4. model rollback: serve the previous model version, then restore"
ACTIVE="$(sql "SELECT id FROM model_versions WHERE status = 'active'")"
PREV="$(sql "SELECT max(id) FROM model_versions WHERE status = 'archived' AND id < $ACTIVE")"
echo "active=$ACTIVE previous=$PREV"
sql "BEGIN; UPDATE model_versions SET status = 'archived' WHERE status = 'active'; UPDATE model_versions SET status = 'active' WHERE id = $PREV; COMMIT;" > /dev/null
echo "after rollback:  $(health)"
sql "BEGIN; UPDATE model_versions SET status = 'archived' WHERE status = 'active'; UPDATE model_versions SET status = 'active' WHERE id = $ACTIVE; COMMIT;" > /dev/null
echo "after restoring: $(health)"

step "5. backup, restore into a scratch database, compare"
DUMP="$(mktemp)"
docker exec -i "$DB" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc < /dev/null > "$DUMP"
echo "dump size: $(du -h "$DUMP" | cut -f1)"
sql "DROP DATABASE IF EXISTS parse_restore_drill" postgres > /dev/null
sql "CREATE DATABASE parse_restore_drill" postgres > /dev/null
docker exec -i "$DB" pg_restore -U "$POSTGRES_USER" -d parse_restore_drill --no-owner < "$DUMP"
COUNTS="SELECT (SELECT count(*) FROM raw_feedback), (SELECT count(*) FROM feedback), (SELECT count(*) FROM quarantine), (SELECT count(*) FROM embeddings), (SELECT count(*) FROM labels), (SELECT count(*) FROM taxonomy_nodes), (SELECT count(*) FROM model_versions), (SELECT md5(string_agg(artifact_sha256, ',' ORDER BY id)) FROM model_versions)"
LIVE="$(sql "$COUNTS")"; RESTORED="$(sql "$COUNTS" parse_restore_drill)"
echo "live:     $LIVE"; echo "restored: $RESTORED"
[ "$LIVE" = "$RESTORED" ] || { echo "FAIL: restored database differs" >&2; exit 1; }
echo "triggers survive the restore: $(sql "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal" parse_restore_drill) triggers"
docker exec -i "$DB" psql -U "$POSTGRES_USER" -d parse_restore_drill -Atc "DELETE FROM raw_feedback" < /dev/null 2>&1 | tail -1 || true
sql "DROP DATABASE parse_restore_drill" postgres > /dev/null
rm -f "$DUMP"

echo; echo "DRILL OK"
