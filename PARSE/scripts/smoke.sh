#!/usr/bin/env bash
# Verify a running stack from the outside, through the public entry point (nginx).
# usage: scripts/smoke.sh [base_url]     tokens come from the environment or .env
set -euo pipefail
BASE="${1:-http://127.0.0.1:8141}"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
ADMIN="${ADMIN_TOKEN:?ADMIN_TOKEN not set}"
ANNOTATOR="${ANNOTATOR_TOKEN:?ANNOTATOR_TOKEN not set}"
fail() { echo "FAIL: $*" >&2; exit 1; }
get() { curl -fsS --max-time 20 -H "Authorization: Bearer $2" "$BASE$1"; }
py() { if command -v python3 >/dev/null 2>&1; then python3 "$@"; else python "$@"; fi; }
json() { py -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

echo "1. web serves the app"
curl -fsS --max-time 10 "$BASE/" | grep -q '<div id="root">' || fail "index.html not served"

echo "2. health exercises db, model and embedder"
curl -fsS --max-time 20 "$BASE/api/v1/health" | json "d['status'], {k: v['ok'] for k, v in d['checks'].items()}"
[ "$(curl -fsS "$BASE/api/v1/health" | json "d['checks']['database']['ok'] and d['checks']['model']['ok']")" = "True" ] || fail "database or model not ok"

echo "3. auth is enforced"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/v1/stats")" = "401" ] || fail "unauthenticated request was not rejected"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $ANNOTATOR" "$BASE/api/v1/quarantine")" = "403" ] || fail "annotator reached an admin endpoint"

echo "4. /metrics is not exposed publicly"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/metrics")" != "200" ] || curl -s "$BASE/metrics" | grep -q '<div id="root">' || fail "/metrics reachable from outside"

echo "5. data is seeded"
get /api/v1/stats "$ADMIN" | json "'pool', d['pool'], 'test', d['test'], 'labelled', d['labelled'], 'model', d['active_model']"

echo "6. the labelling queue has items with suggestions"
[ "$(get '/api/v1/queue?limit=3' "$ANNOTATOR" | json "len(d['items']) == 3 and all(i['suggestions'] for i in d['items'])")" = "True" ] || fail "queue empty or without suggestions"

echo "7. classification is consistent with the taxonomy"
curl -fsS --max-time 30 -X POST -H "Authorization: Bearer $ANNOTATOR" -H 'Content-Type: application/json' \
  -d '{"texts": ["The room was dirty and the staff were rude", "La batería del portátil dura dos horas"]}' \
  "$BASE/api/v1/classify" | json "[(r['route'], r['confidence'], [l['path'] for l in r['labels']]) for r in d['results']]"

echo "OK: stack at $BASE is up and serving"
