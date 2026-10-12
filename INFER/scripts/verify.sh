#!/usr/bin/env bash
# Verify a running deployment from the outside, over HTTP only.
#   scripts/verify.sh <base-url> <read-token> <admin-token>
# Exits non-zero on the first expectation that does not hold.
set -euo pipefail

BASE="${1:?base url}"; READ="${2:?read token}"; ADMIN="${3:?admin token}"
pass=0

body=""
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
expect() { # expect <description> <expected-status> <curl args...>
  local what="$1" want="$2"; shift 2
  local got
  got=$(curl -s -o "$tmp" -w '%{http_code}' "$@")
  body=$(cat "$tmp")
  if [ "$got" != "$want" ]; then
    echo "FAIL  $what: expected $want, got $got"; echo "${body:0:400}"; exit 1
  fi
  echo "ok    $what ($got)"; pass=$((pass + 1))
}
field() { # field <name>: first string or number field with that name in the last response body
  echo "$body" | grep -o "\"$1\":\"\?[^\",}]*" | head -1 | sed 's/.*:"\?//'
}
rd=(-H "Authorization: Bearer $READ"); adm=(-H "Authorization: Bearer $ADMIN"); js=(-H 'content-type: application/json')

expect "liveness"                            200 "$BASE/healthz"
expect "readiness exercises dependencies"    200 "$BASE/readyz"
expect "the dashboard"                       200 "$BASE/"
expect "results without a token"             401 "$BASE/v1/datasets"
expect "results with a wrong token"          401 -H "Authorization: Bearer wrong" "$BASE/v1/datasets"
expect "datasets"                            200 "${rd[@]}" "$BASE/v1/datasets"
expect "unknown dataset"                     404 "${rd[@]}" "$BASE/v1/datasets/nope/experiment"
expect "model comparison"                    200 "${rd[@]}" "$BASE/v1/datasets/synthetic/experiment"
experiment=$(field id)
# the champion's id is the "id" that precedes "is_champion":true for that run
run=$(echo "$body" | grep -o '"id":"[^"]*","learner":"[^"]*","status":"complete","error":null,"is_champion":true' | cut -d'"' -f4)
[ -n "$run" ] || { echo "FAIL  no champion in the experiment"; exit 1; }
expect "deciles"                             200 "${rd[@]}" "$BASE/v1/runs/$run/deciles"
expect "profiles"                            200 "${rd[@]}" "$BASE/v1/runs/$run/profiles"
expect "segments that hurt"                  200 "${rd[@]}" "$BASE/v1/experiments/$experiment/segments?verdict=hurts"
echo "$body" | grep -q '"verdict":"hurts"' || { echo "FAIL  the planted harmful segment was not found"; exit 1; }
expect "scores, first page"                  200 "${rd[@]}" "$BASE/v1/runs/$run/scores?limit=5"
expect "scores with a forged cursor"         400 "${rd[@]}" "$BASE/v1/runs/$run/scores?cursor=bm9wZQ=="
expect "online prediction"                   200 -X POST "${rd[@]}" "${js[@]}" -d '{"units":[{"recency":2,"history":60,"newbie":true,"channel":"Web"}]}' "$BASE/v1/runs/$run/predict"
expect "prediction with an invalid unit"     422 -X POST "${rd[@]}" "${js[@]}" -d '{"units":[{"recency":99}]}' "$BASE/v1/runs/$run/predict"
key="verify-$(date +%s)-$RANDOM"
policy="{\"run_id\":\"$run\",\"budget\":500,\"cost_per_treatment\":1,\"value_per_outcome\":20}"
expect "policy with the read token"          403 -X POST "${rd[@]}" "${js[@]}" -H "Idempotency-Key: $key" -d "$policy" "$BASE/v1/policies"
expect "policy without an idempotency key"   422 -X POST "${adm[@]}" "${js[@]}" -d "$policy" "$BASE/v1/policies"
expect "create a policy"                     201 -X POST "${adm[@]}" "${js[@]}" -H "Idempotency-Key: $key" -d "$policy" "$BASE/v1/policies"
id=$(field id); targeted=$(field n_targeted)
expect "retry the same request"              200 -X POST "${adm[@]}" "${js[@]}" -H "Idempotency-Key: $key" -d "$policy" "$BASE/v1/policies"
[ "$(field id)" = "$id" ] || { echo "FAIL  the retry created a second policy"; exit 1; }
expect "same key, different request"         409 -X POST "${adm[@]}" "${js[@]}" -H "Idempotency-Key: $key" -d "${policy/500/600}" "$BASE/v1/policies"
expect "read the policy"                     200 "${rd[@]}" "$BASE/v1/policies/$id"
expect "export with the read token"          403 "${rd[@]}" "$BASE/v1/policies/$id/export"
expect "export the activation file"          200 "${adm[@]}" "$BASE/v1/policies/$id/export"
lines=$(($(echo "$body" | wc -l) - 1))
[ "$lines" = "$targeted" ] || { echo "FAIL  export has $lines units, policy targets $targeted"; exit 1; }
[ "$targeted" -le 500 ] || { echo "FAIL  policy targets $targeted units on a budget that pays for 500"; exit 1; }
echo "ok    export lists exactly the $targeted targeted units, within budget"

echo "verified: $pass checks passed against $BASE"
