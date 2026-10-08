"""Deployment packages: one reproducible tar.gz per hardware target.

A package is everything a board needs to run and to prove what it ran: the models that passed the
accuracy gate, a slice of the evaluation set with the predictions the build host got on it, the
benchmark harness, and a manifest with a sha256 for every file. Building twice from the same run
gives byte-identical archives (fixed order, timestamps, owners and gzip header).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import numpy as np

from . import data, quant

HARNESS = Path(__file__).resolve().parent.parent / "device" / "edge_bench.py"
TARGETS = Path(__file__).resolve().parent.parent / "targets.json"
PACK_N = 100

RUN_SH = """#!/bin/sh
# Benchmark every model in this package on this board and write results.jsonl next to it.
# Extra arguments go to the harness, e.g.:  ./run.sh --post https://host --power cmd:'./read_meter'
set -eu
cd "$(dirname "$0")"
exec python3 edge_bench.py --package . "$@"
"""


def targets() -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in json.loads(TARGETS.read_text())}


def _npy(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, arr)
    return buf.getvalue()


def build(run_dir: Path, ds: data.Dataset, target: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Returns the package record (also written next to the archive as <name>.package.json)."""
    run = json.loads((run_dir / "run.json").read_text())
    eval_idx = ds.idx("eval")
    # Every 5th evaluation image in filename order: all classes, no randomness.
    pack = eval_idx[:: max(1, len(eval_idx) // PACK_N)][:PACK_N]
    images, labels = np.asarray(ds.images[pack]), ds.labels[pack].astype(np.int64)

    files: dict[str, bytes] = {
        "eval_images.npy": _npy(images),
        "eval_labels.npy": _npy(labels),
        "edge_bench.py": HARNESS.read_bytes().replace(b"\r\n", b"\n"),
        "run.sh": RUN_SH.encode(),
    }
    variants = []
    limit = target.get("max_model_mb")
    for v in run["variants"]:
        if v["gate"] != "pass" or (limit and v["size_bytes"] > limit * 2**20):
            continue
        path = run_dir / v["file"]
        files[f"models/{v['file']}"] = path.read_bytes()
        expected = quant.logits(path, images, np.arange(len(images))).argmax(1)
        variants.append(
            {
                "name": v["name"],
                "file": f"models/{v['file']}",
                "sha256": v["sha256"],
                "size_bytes": v["size_bytes"],
                "precision": v["precision"],
                "technique": v["technique"],
                "top1_host": v["eval"]["top1"],
                "expected": [int(p) for p in expected],
            }
        )
    manifest = {
        "schema": 1,
        "kind": "package",
        "name": f"squeeze-{target['name']}-{run['run_id']}",
        "target": target,
        "run_id": run["run_id"],
        "git_commit": run["git_commit"],
        "dataset_version": run["dataset"]["version"],
        "classes": [name for _, name, _ in data.CLASSES],
        "input": {
            "name": "input",
            "dtype": "float32",
            "layout": "NCHW",
            "size": run["config"]["size"],
            "range": "RGB 0..255",
        },
        "eval_pack": {"images": "eval_images.npy", "labels": "eval_labels.npy", "n": len(pack)},
        "variants": variants,
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(files.items())},
    }
    files["manifest.json"] = json.dumps(manifest, indent=1, sort_keys=True).encode()

    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{manifest['name']}.tar.gz"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            info = tarfile.TarInfo(f"{manifest['name']}/{name}")
            info.size, info.mtime, info.mode = len(files[name]), 0, 0o755 if name == "run.sh" else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(files[name]))
    with archive.open("wb") as sink, gzip.GzipFile(fileobj=sink, mode="wb", mtime=0, filename="") as gz:
        gz.write(raw.getvalue())
    record = {
        "schema": 1,
        "kind": "package",
        "package_id": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "filename": archive.name,
        "size_bytes": archive.stat().st_size,
        "manifest": {k: v for k, v in manifest.items() if k != "files"},
    }
    for v in record["manifest"]["variants"]:  # type: ignore[index]
        v.pop("expected")
    (out_dir / f"{manifest['name']}.package.json").write_text(json.dumps(record, indent=1, sort_keys=True))
    return record
