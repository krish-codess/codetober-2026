import dataclasses
import hashlib
import json

import pytest

from squeeze import config


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "pk").mkdir()
    return dataclasses.replace(
        config.load(),
        db_path=tmp_path / "t.db",
        packages_dir=tmp_path / "pk",
        raw_dir=tmp_path / "raw",
        web_dir=tmp_path / "no-web",
        device_token="dev-token",
        admin_token="admin-token",
    )


@pytest.fixture(scope="session")
def smoke_run(tmp_path_factory):
    """The whole pipeline, tiny, on generated data with planted defects. (root, dataset, record)"""
    pytest.importorskip("torch")
    from squeeze import data, pipeline, synth

    root = tmp_path_factory.mktemp("pipe")
    synth.dataset(root / "raw", seed=0, per_class=40, size=64)
    ds = data.build(root / "raw", root / "ds", size=64)
    return root, ds, pipeline.execute(pipeline.SMOKE, ds, root / "runs")


def fake_run(run_id="run0", created_at="2026-10-08T10:00:00Z"):
    """A pipeline run record with the shape pipeline.execute writes, small enough to read."""

    def variant(name, parent, technique, precision, top1, gate="pass", size=1000):
        return {
            "name": name, "parent": parent, "technique": technique, "arch": "net", "precision": precision,
            "params": 100, "file": f"{name}.onnx", "sha256": (name.encode().hex() + "0" * 64)[:64], "size_bytes": size,
            "eval": {"n": 500, "top1": top1, "lo": top1 - 0.02, "hi": min(1.0, top1 + 0.02)},
            "eval_deploy": {"n": 500, "top1": top1 - 0.03, "lo": 0.0, "hi": 1.0},
            "vs_parent": None if parent is None else {"agree": 0.99, "delta": -0.01, "lo": -0.02, "hi": 0.0},
            "detail": {}, "gate": gate, "gate_reason": "test",
        }  # fmt: skip

    return {
        "schema": 1, "kind": "pipeline", "run_id": run_id, "created_at": created_at, "git_commit": "abc",
        "config": {"budget_pt": 2.0}, "dataset": {"version": "d1", "profile": {}},
        "baselines": [{"name": "majority class", "n": 500, "top1": 0.1, "lo": 0.08, "hi": 0.13}],
        "variants": [
            variant("teacher", None, "none", "fp32", 0.98, size=9000),
            variant("student", "teacher", "distillation", "fp32", 0.97),
            variant("student-int8", "student", "quantization", "int8", 0.60, gate="fail"),
            variant("student-mixed", "student", "quantization-mixed", "int8+fp32", 0.965),
        ],
        "sensitivity": {
            "variant": "student",
            "rows": [
                {"node": "conv0", "op_type": "Conv", "rank": 1, "kl": 3.2, "top1_drop": 0.8, "kept_float": True},
                {"node": "conv1", "op_type": "Conv", "rank": 2, "kl": 0.01, "top1_drop": 0.0, "kept_float": False},
            ],
        },
        "calibration_study": [],
    }  # fmt: skip


def bench_payload(variant="student", target="rpi5", p50=20.0, power=True, **over):
    """A device result with the shape device/edge_bench.py writes."""
    body = {
        "schema": 1, "target": target, "variant": variant, "model_sha256": (variant.encode().hex() + "0" * 64)[:64],
        "runtime": "onnxruntime", "runtime_version": "1.30.0", "provider": "CPUExecutionProvider", "threads": 4,
        "measured_at": "2026-10-08T12:00:00+00:00",
        "device": {"machine": "aarch64", "cpu_model": "Cortex-A76", "cores": 4, "os": "Linux", "board": "Pi 5"},
        "latency_ms": {"n": 200, "p50": p50, "p95": p50 * 1.2, "p99": p50 * 1.5, "mean": p50 * 1.05},
        "accuracy": {"n": 100, "top1": 0.97, "agree_host": 1.0},
        "power": {"source": "pmic", "unit": "W", "idle_w": 2.7, "load_w": 5.1, "energy_mj": 48.0, "samples": 40}
        if power else None,
        **over,
    }  # fmt: skip
    body["result_id"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
    return body
