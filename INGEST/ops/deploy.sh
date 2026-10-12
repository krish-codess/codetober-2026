#!/usr/bin/env bash
# Deploy, verify from outside, and roll back automatically if verification fails.
#
#   ops/deploy.sh user@vm.example.com v1.2.0 https://fleet.example.com     deploy tag v1.2.0
#   ops/deploy.sh user@vm.example.com --rollback https://fleet.example.com  return to the previous tag
#
# The VM needs Docker with the compose plugin, a checkout of this repository at ~/ghostfleet/INGEST,
# and a filled-in .env there. Images are built on the VM from the tagged source: no registry to run.
set -euo pipefail

host=${1:?usage: deploy.sh user@host <git-tag>|--rollback <public-url>}
target=${2:?missing git tag or --rollback}
url=${3:?missing public URL to verify against}
dir='~/ghostfleet/INGEST'

remote() { ssh -o BatchMode=yes "$host" "cd $dir && $*"; }

verify() {
  # From this machine, not from the VM: proves the thing is reachable by its users, and that it is
  # the database answering, not a static page. /readyz runs a real query.
  for attempt in $(seq 1 30); do
    if body=$(curl -fsS --max-time 10 "$url/readyz"); then
      echo "ready: $body"
      curl -fsS --max-time 10 "$url/api/v1/stats" > /dev/null && return 0
    fi
    sleep 10
  done
  return 1
}

release() {
  local tag=$1
  remote "git fetch --tags --quiet && git checkout --quiet $tag"
  # Migrations run in the init container, before any service starts on the new code.
  remote "GHOST_TAG=$tag docker compose up --build --detach --remove-orphans --wait --wait-timeout 300"
  remote "echo $tag > .deployed"
}

previous=$(remote "cat .deployed.previous 2>/dev/null || true")
current=$(remote "cat .deployed 2>/dev/null || true")

if [ "$target" = "--rollback" ]; then
  [ -n "$previous" ] || { echo "no previous release recorded on $host" >&2; exit 1; }
  echo "rolling back $current -> $previous"
  # Schema first: migrations are backward-compatible by policy (docs/operations.md), so the old code
  # runs on the new schema. Roll the schema back by hand only if a release note says to.
  release "$previous"
  verify || { echo "rollback did not come up healthy" >&2; exit 1; }
  exit 0
fi

echo "deploying $target (currently: ${current:-nothing})"
[ -n "$current" ] && remote "echo $current > .deployed.previous"
release "$target"
if verify; then
  echo "deployed $target"
else
  echo "verification failed; rolling back to ${current:-nothing}" >&2
  [ -n "$current" ] && release "$current" && verify
  exit 1
fi
