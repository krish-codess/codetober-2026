#!/usr/bin/env bash
# Verify a running deployment from the outside, over HTTP only.
#   scripts/verify.sh <base-url> <user-token> <admin-token>
# Exits non-zero on the first expectation that does not hold.
set -euo pipefail

BASE="${1:?base url}"; TOKEN="${2:?user token}"; ADMIN="${3:?admin token}"
pass=0

body=""
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
expect() { # expect <description> <expected-status> <curl args...>
  local what="$1" want="$2"; shift 2
  local got
  got=$(curl -s -o "$tmp" -w '%{http_code}' "$@")
  body=$(tr -d '\0' < "$tmp")  # the image response is binary
  if [ "$got" != "$want" ]; then
    echo "FAIL  $what: expected $want, got $got"; echo "${body:0:400}"; exit 1
  fi
  echo "ok    $what ($got)"; pass=$((pass + 1))
}
field() { # field <name> [occurrence]: a string field of the last response body, no JSON tooling needed
  echo "$body" | grep -o "\"$1\":\"[^\"]*\"" | sed -n "${2:-1}p" | cut -d'"' -f4
}
auth=(-H "Authorization: Bearer $TOKEN"); adm=(-H "Authorization: Bearer $ADMIN"); js=(-H 'content-type: application/json')

expect "liveness"                         200 "$BASE/healthz"
expect "readiness exercises dependencies" 200 "$BASE/readyz"
expect "story without a token"            401 "$BASE/v1/wrapped"
expect "story with a forged token"        401 -H "Authorization: Bearer forged.token" "$BASE/v1/wrapped"
expect "story"                            200 "${auth[@]}" "$BASE/v1/wrapped"
card=$(field type 2)
etag=$(curl -s -o /dev/null -D - "${auth[@]}" "$BASE/v1/wrapped" | tr -d '\r' | awk 'tolower($1)=="etag:"{print $2}')
expect "story revalidation"               304 "${auth[@]}" -H "If-None-Match: $etag" "$BASE/v1/wrapped"
expect "record a view"                    204 -X PUT "${auth[@]}" "$BASE/v1/wrapped/views/$card"
expect "record the same view again"       204 -X PUT "${auth[@]}" "$BASE/v1/wrapped/views/$card"
expect "view of a card not in the story"  404 -X PUT "${auth[@]}" "$BASE/v1/wrapped/views/no_such_card"
expect "view with a malformed card name"  422 -X PUT "${auth[@]}" "$BASE/v1/wrapped/views/NOT-VALID"
expect "share with a malformed body"      422 -X POST "${auth[@]}" "${js[@]}" -d '{"card":1}' "$BASE/v1/wrapped/shares"
expect "share a card that is not shareable" 409 -X POST "${auth[@]}" "${js[@]}" -d '{"card_type":"intro"}' "$BASE/v1/wrapped/shares"
body=$(curl -s -X POST "${auth[@]}" "${js[@]}" -d '{"card_type":"summary"}' "$BASE/v1/wrapped/shares")
share=$(field share_id)
expect "share again returns the same share" 200 -X POST "${auth[@]}" "${js[@]}" -d '{"card_type":"summary"}' "$BASE/v1/wrapped/shares"
[ "$(field share_id)" = "$share" ] || { echo "FAIL  retry created a second share"; exit 1; }
expect "public share"                     200 "$BASE/v1/shares/$share"
expect "public share image"               200 "$BASE/v1/shares/$share/card.png"
expect "share landing page"               200 "$BASE/s/$share"
expect "unknown share"                    404 "$BASE/v1/shares/AAAAAAAAAAAAAAAAAAAAAA"
expect "admin route without a token"      401 "$BASE/v1/admin/analytics/share-rate"
expect "admin route with a user token"    403 "${auth[@]}" "$BASE/v1/admin/analytics/share-rate"
expect "superlative distribution"         200 "${adm[@]}" "$BASE/v1/admin/analytics/superlatives"
expect "share rate by card type"          200 "${adm[@]}" "$BASE/v1/admin/analytics/share-rate"
expect "payload listing, first page"      200 "${adm[@]}" "$BASE/v1/admin/payloads?limit=2"
cursor=$(field next_cursor)
expect "payload listing, next page"       200 "${adm[@]}" "$BASE/v1/admin/payloads?limit=2&cursor=$cursor"
expect "payload listing, bad cursor"      422 "${adm[@]}" "$BASE/v1/admin/payloads?cursor=%%%"
expect "payload listing, limit too large" 422 "${adm[@]}" "$BASE/v1/admin/payloads?limit=100000"

echo "verified: $pass checks passed against $BASE"
