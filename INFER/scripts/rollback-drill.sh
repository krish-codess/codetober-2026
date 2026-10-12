#!/usr/bin/env bash
# Prove that rollback works by doing it, against the running compose stack:
#   1. note the experiment being served
#   2. train and score a second experiment on the same data
#   3. confirm the API now serves the new one
#   4. retire it; confirm the API serves the first again, readiness passes, and a policy can still be built
# Leaves the stack serving the original experiment.
#   scripts/rollback-drill.sh <base-url> <read-token> <admin-token>
set -euo pipefail
BASE="${1:-http://localhost:8080}"; READ="${2:?read token}"; ADMIN="${3:?admin token}"

serving() {
  curl -fsS -H "Authorization: Bearer $READ" "$BASE/v1/datasets/synthetic/experiment" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4
}

first=$(serving)
echo "serving before:   $first"

docker compose run --rm -T batch train --dataset synthetic --learners response t_learner x_learner > /dev/null
docker compose run --rm -T batch score --dataset synthetic > /dev/null
second=$(serving)
echo "after retraining: $second"
[ "$second" != "$first" ] || { echo "FAIL  a new experiment did not change what is served"; exit 1; }

docker compose run --rm -T batch retire "$second" > /dev/null
back=$(serving)
echo "after rollback:   $back"
[ "$back" = "$first" ] || { echo "FAIL  rollback did not restore the previous experiment"; exit 1; }

bash "$(dirname "$0")/verify.sh" "$BASE" "$READ" "$ADMIN" > /dev/null || { echo "FAIL  the stack does not verify after rollback"; exit 1; }
echo "rollback drill passed"
