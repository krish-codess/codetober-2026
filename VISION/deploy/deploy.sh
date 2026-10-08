#!/bin/sh
# Deploy a package to a board over SSH, benchmark it there, and bring the results back.
#
#   deploy/deploy.sh <package.tar.gz> <user@host> [harness arguments...]
#   deploy/deploy.sh squeeze-rpi5-0123abcd.tar.gz pi@raspberrypi.local --power pmic
#
# The board needs python3 with numpy and onnxruntime (pip install numpy onnxruntime).
# Each package is unpacked into its own directory under ~/squeeze/releases and `current` is
# switched to it only after the archive's checksum and the board's runtime have been verified,
# so a failed deploy leaves the previous release in place. Roll back with deploy/rollback.sh.
set -eu
[ $# -ge 2 ] || { sed -n '2,10p' "$0"; exit 2; }
archive=$1; host=$2; shift 2
name=$(basename "$archive" .tar.gz)
sum=$(sha256sum "$archive" | cut -d' ' -f1)

echo ">> copying $name ($sum)"
ssh "$host" 'mkdir -p ~/squeeze/releases ~/squeeze/incoming'
scp -q "$archive" "$host:squeeze/incoming/$name.tar.gz"

ssh "$host" sh -eu -s "$name" "$sum" <<'REMOTE'
name=$1; sum=$2; cd ~/squeeze
echo "$sum  incoming/$name.tar.gz" | sha256sum -c - >/dev/null || { echo "checksum mismatch after copy; nothing changed" >&2; exit 1; }
python3 -c 'import numpy, onnxruntime' || { echo "python3 needs numpy and onnxruntime; nothing changed" >&2; exit 1; }
rm -rf "releases/$name" && tar -xzf "incoming/$name.tar.gz" -C releases && rm "incoming/$name.tar.gz"
[ -L current ] && ln -sfn "$(readlink current)" previous
ln -sfn "releases/$name" current
echo ">> $name is current on $(hostname)"
REMOTE

echo ">> benchmarking on the device"
ssh "$host" "cd ~/squeeze/current && rm -f results.jsonl && sh run.sh $*" || echo "!! some models failed on the device (see above); collecting what ran" >&2
mkdir -p results/raw
out="results/raw/bench-$name-$(echo "$host" | tr -c 'A-Za-z0-9\n' '_').jsonl"
scp -q "$host:squeeze/current/results.jsonl" "$out"
echo ">> results in $out ; load them with: python -m squeeze ingest"
