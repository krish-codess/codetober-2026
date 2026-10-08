"""Generators with the same shape and the same defects as the real inputs.

`dataset` writes a small image folder laid out like Imagenette, with every defect the validator is
meant to catch planted in it. `bench_results` (below) does the same for device result uploads.
Both feed the same code paths as the real sources; tests and CI run on them.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image

from .data import CLASSES, EVAL_PREFIX


def _image(rng: np.random.Generator, label: int, size: int) -> Image.Image:
    """Learnable but not trivial: the class sets the hue and stripe frequency, noise does the rest."""
    yy, xx = np.mgrid[0:size, 0:size] / size
    base = np.stack([np.sin((label + 1) * 3 * xx + c * 2.1 + label) for c in range(3)], -1) * 0.5 + 0.5
    noisy = base * 0.7 + rng.random((size, size, 3)) * 0.3 + yy[..., None] * 0.0
    return Image.fromarray((np.clip(noisy, 0, 1) * 255).astype(np.uint8))


def _jpeg(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def dataset(root: Path, seed: int = 0, per_class: int = 24, size: int = 64, defects: bool = True) -> dict[str, str]:
    """Returns {relative path: expected quarantine reason or kept-flag} for every planted defect."""
    rng = np.random.default_rng(seed)
    for label, (wnid, _, _) in enumerate(CLASSES):
        for i in range(per_class):
            folder = "val" if i % 3 == 0 else "train"
            # A quarter of the files carry ImageNet-validation provenance, in both folders.
            name = f"{EVAL_PREFIX}{label:04d}{i:04d}.JPEG" if i % 4 == 0 else f"{wnid}_{i}.JPEG"
            path = root / folder / wnid / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_jpeg(_image(rng, label, size)))
    planted: dict[str, str] = {}
    if not defects:
        return planted
    a, b = CLASSES[0][0], CLASSES[1][0]
    good = _jpeg(_image(rng, 0, size))

    def plant(rel: str, raw: bytes, expect: str) -> None:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(raw)
        planted[rel] = expect

    plant(f"train/{a}/{a}_9001.JPEG", b"", "empty")
    plant(f"train/{a}/{a}_9002.JPEG", good[: len(good) // 3], "undecodable")
    plant(f"train/{a}/{a}_9003.JPEG", b"not a jpeg at all", "undecodable")
    plant(f"train/{a}/{a}_9004.JPEG", _jpeg(_image(rng, 0, 8)), "too_small")
    plant(f"train/{b}/{a}_9005.JPEG", good, "label_conflict")
    plant("train/n99999999/n99999999_1.JPEG", good, "unknown_class")
    plant("train/.DS_Store", b"\x00\x00\x00\x01Bud1", "not_an_image")
    plant("noisy_imagenette.csv", b"path,noisy_labels_0\n", "not_an_image")
    first = sorted((root / "train" / a).glob(f"{a}_*.JPEG"))[0]
    plant(f"val/{a}/{a}_9006.JPEG", first.read_bytes(), "duplicate_of")
    plant(f"train/{a}/{a}_9007.JPEG", _jpeg(_image(rng, 0, size).convert("L")), "converted:L")
    plant(f"train/{a}/{a}_9008.JPEG", _jpeg(_image(rng, 0, size).convert("CMYK")), "converted:CMYK")
    return planted


def bench_results(
    variants: list[dict[str, str]], targets: list[str], n: int, seed: int = 0, defects: bool = True
) -> list[tuple[dict[str, object], str]]:
    """Device result uploads shaped like the real ones, marked `synthetic`, for load tests and for
    exercising the ingest boundary. Returns (payload, expected outcome) pairs. With `defects`,
    roughly one in eight payloads carries a fault real devices produce: a resend, a clock that was
    never set, milliwatts, a harness bug, a typo in the target."""
    import hashlib
    import json
    import random
    from datetime import UTC, datetime, timedelta

    rng = random.Random(seed)  # noqa: S311
    base = datetime(2026, 1, 1, tzinfo=UTC)
    out: list[tuple[dict[str, object], str]] = []
    while len(out) < n:
        v = rng.choice(variants)
        target = rng.choice(targets)
        speed = (int(hashlib.sha256((target + v["name"]).encode()).hexdigest()[:4], 16) % 190 + 10) / 2
        p50 = speed * rng.lognormvariate(0, 0.08)
        has_power = rng.random() < 0.6
        idle = rng.uniform(2.5, 3.2)
        body: dict[str, object] = {
            "schema": 1,
            "synthetic": True,
            "target": target,
            "variant": v["name"],
            "model_sha256": v["sha256"],
            "runtime": "onnxruntime",
            "runtime_version": "1.30.0",
            "provider": "CPUExecutionProvider",
            "threads": 4,
            "measured_at": (base + timedelta(seconds=len(out) * 37)).isoformat(),
            "device": {"machine": "aarch64", "cpu_model": "Cortex-A76", "cores": 4, "os": "Linux", "board": None},
            "latency_ms": {"n": 200, "p50": p50, "p95": p50 * 1.15, "p99": p50 * 1.4, "mean": p50 * 1.03},
            "accuracy": {"n": 100, "top1": 0.97, "agree_host": 1.0},
            "power": {
                "source": "pmic", "unit": "W", "idle_w": idle, "load_w": idle + 2.4,
                "energy_mj": 2.4 * p50, "samples": 40,
            } if has_power else None,
        }  # fmt: skip
        if not has_power:
            body["power_unavailable"] = "no sensor"
        expect = "created"
        fault = rng.randrange(64) if defects else 99
        if fault == 0 and out:  # the device retried after a timeout
            out.append((dict(out[-1][0]), "duplicate" if out[-1][1] == "created" else out[-1][1]))
            continue
        if fault == 1:
            body["measured_at"] = int((base + timedelta(seconds=len(out))).timestamp())  # epoch seconds: fine
        elif fault == 2:
            body["measured_at"] = "2026-10-03T12:00:00"  # naive: taken as UTC
        elif fault == 3 and has_power:
            body["power"] = {
                "source": "pmic", "unit": "mW", "idle_w": idle * 1000, "load_w": (idle + 2.4) * 1000,
                "energy_mj": 2.4 * p50, "samples": 40,
            }  # fmt: skip
        elif fault == 4:
            body["measured_at"], expect = "1970-01-01T00:03:20+00:00", "quarantined"  # clock never set
        elif fault == 5:
            body["latency_ms"], expect = (
                {"n": 200, "p50": 50.0, "p95": 20.0, "p99": 60.0, "mean": 50.0},
                "quarantined",
            )
        elif fault == 6:
            body["latency_ms"], expect = (
                {"n": 200, "p50": float("nan"), "p95": 1.0, "p99": 1.0, "mean": 1.0},
                "quarantined",
            )
        elif fault == 7:
            body["target"], expect = "rpi-5", "quarantined"  # typo: not a known target
        elif fault == 8:
            del body["accuracy"]
            expect = "quarantined"
        body["result_id"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
        out.append((body, expect))
    return out
