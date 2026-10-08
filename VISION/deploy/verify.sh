#!/bin/sh
# Check a running deployment from outside, over HTTP only.   deploy/verify.sh http://localhost:8080
set -eu
base=${1:?usage: verify.sh <base url>}
fail() { echo "FAIL: $*" >&2; exit 1; }
curl -sf "$base/healthz" | grep -q '"status":"ok"' || fail "healthz is not ok"
curl -sf "$base/v1/targets" | grep -q '"name":"rpi5"' || fail "targets missing"
curl -sf "$base/v1/tradeoff?target=x86-laptop" | grep -q '"name":"teacher-r50"' || fail "seeded run missing from the tradeoff"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$base/v1/results" -d '{}')" = 401 ] || fail "upload without a token was not refused"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$base/v1/tradeoff?target=nope")" = 404 ] || fail "unknown target is not a 404"
curl -sf "$base/" | grep -q '<div id="root">' || fail "web UI not served"
curl -sf "$base/metrics" | grep -q squeeze_http_requests_total || fail "metrics missing"
echo "ok: $base"
