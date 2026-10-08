#!/bin/sh
# Switch a board back to the release that was current before the last deploy.
#   deploy/rollback.sh <user@host>
set -eu
[ $# -eq 1 ] || { echo "usage: $0 <user@host>" >&2; exit 2; }
ssh "$1" sh -eu <<'REMOTE'
cd ~/squeeze
[ -L previous ] || { echo "no previous release recorded; nothing changed" >&2; exit 1; }
was=$(readlink current); ln -sfn "$(readlink previous)" current; ln -sfn "$was" previous
echo "current is now $(readlink current) (was $was)"
REMOTE
