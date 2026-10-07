#!/usr/bin/env bash
# Prove that rollback works by doing it, against the running compose stack:
#   1. note the run being served
#   2. build and publish a second run (same data, stricter k, so it is a different run)
#   3. confirm the API now serves the new run
#   4. roll back; confirm the API serves the first run again and still passes readiness
# Leaves the stack serving the original run.
set -euo pipefail
BASE="${1:-http://localhost:8080}"

# /readyz is not proxied by the edge on purpose; ask the API container itself.
serving() {
  docker compose exec -T api python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/readyz'))['checks']['active_run'])"
}

first=$(serving)
echo "serving before:   $first"

docker compose run --rm -T -e WRAPPED_K_ANONYMITY=25 batch run-all > /dev/null
second=$(serving)
echo "after publish:    $second"
[ "$second" != "$first" ] || { echo "FAIL  publishing a new run did not change what is served"; exit 1; }

docker compose run --rm -T batch rollback > /dev/null
back=$(serving)
echo "after rollback:   $back"
[ "$back" = "$first" ] || { echo "FAIL  rollback did not restore the previous run"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")
[ "$code" = "200" ] || { echo "FAIL  UI returned $code after rollback"; exit 1; }
echo "rollback drill passed"
