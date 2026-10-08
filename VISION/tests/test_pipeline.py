"""Model tests on generated data: training smoke test, export/inference contract, resumability,
reproducible packaging."""

import hashlib
import json
import tarfile

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from squeeze import config, models, package, pipeline, quant  # noqa: E402

pytestmark = pytest.mark.ml


def test_every_step_was_verified_on_its_exported_artifact(smoke_run):
    record = smoke_run[2]
    names = [v["name"] for v in record["variants"]]
    assert names == [
        "teacher-r50", "student-pretrained", "student-ce", "student-kd", "r50-prune50-raw", "r50-prune50",
        "r50-int8", "r50-prune50-int8", "student-kd-int8", "student-kd-int8-mixed",
    ]  # fmt: skip
    for v in record["variants"]:
        assert v["eval"]["n"] > 0 and v["eval"]["lo"] <= v["eval"]["top1"] <= v["eval"]["hi"]
        assert v["gate"] in ("pass", "fail") and len(v["sha256"]) == 64
        assert (v["parent"] is None) == (v["vs_parent"] is None)
        if "export_max_abs_logit_diff" in v["detail"]:
            assert v["detail"]["export_max_abs_logit_diff"] < 1e-3  # ONNX computes what PyTorch computed


def test_pruning_and_quantization_actually_shrink_the_model(smoke_run):
    by = {v["name"]: v for v in smoke_run[2]["variants"]}
    assert by["r50-prune50"]["params"] < 0.5 * by["teacher-r50"]["params"]
    assert by["r50-int8"]["size_bytes"] < 0.35 * by["teacher-r50"]["size_bytes"]
    sizes = [by[n]["size_bytes"] for n in ("student-kd-int8", "student-kd-int8-mixed", "student-kd")]
    assert sizes == sorted(sizes)


def test_sensitivity_ranks_layers_and_marks_the_ones_kept_float(smoke_run):
    sens = smoke_run[2]["sensitivity"]
    kls = [r["kl"] for r in sens["rows"]]
    assert kls == sorted(kls, reverse=True) and [r["rank"] for r in sens["rows"]] == [1, 2]
    by = {v["name"]: v for v in smoke_run[2]["variants"]}
    kept = [r["node"] for r in sens["rows"] if r["kept_float"]]
    assert kept == by["student-kd-int8-mixed"]["detail"]["float_layers"]


def test_rerun_resumes_from_cache_and_changes_nothing(smoke_run):
    root, ds, record = smoke_run
    steps = root / "runs" / record["run_id"] / "steps"
    stamps = {p.name: p.stat().st_mtime_ns for p in steps.iterdir()}
    again = pipeline.execute(pipeline.SMOKE, ds, root / "runs")
    assert again["variants"] == record["variants"]
    assert stamps == {p.name: p.stat().st_mtime_ns for p in steps.iterdir()}


def test_training_reduces_loss(smoke_run):
    ds = smoke_run[1]
    idx = ds.idx("train", 64)
    net = models.student(pretrained=False)
    soft = np.eye(10, dtype=np.float32)[ds.labels[np.sort(idx)]] * 8
    history = models.train(net, ds.images, ds.labels, idx, teacher_logits=soft, epochs=4, lr=0.05, batch=16)
    assert history[-1] < history[0]


def test_inference_contract(smoke_run):
    root, ds, record = smoke_run
    sess = quant.session(root / "runs" / record["run_id"] / "student-kd-int8-mixed.onnx")
    (inp,), (out,) = sess.get_inputs(), sess.get_outputs()
    assert (inp.name, inp.type, inp.shape[1:]) == ("input", "tensor(float)", [3, 64, 64])
    assert out.name == "logits" and out.shape[1] == 10
    for n in (1, 3):  # dynamic batch
        assert sess.run(None, {"input": quant.to_input(np.asarray(ds.images[:n]))})[0].shape == (n, 10)


def test_packages_are_byte_reproducible_and_self_verifying(smoke_run, tmp_path):
    root, ds, record = smoke_run
    target = config.targets()["ci"]
    a = package.build(root / "runs" / record["run_id"], ds, target, tmp_path / "a")
    b = package.build(root / "runs" / record["run_id"], ds, target, tmp_path / "b")
    assert a["package_id"] == b["package_id"]
    with tarfile.open(tmp_path / "a" / a["filename"]) as tar:
        tar.extractall(tmp_path / "x", filter="data")
    pkg = tmp_path / "x" / a["manifest"]["name"]
    manifest = json.loads((pkg / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((pkg / name).read_bytes()).hexdigest() == digest
    small = {v["name"] for v in record["variants"] if v["size_bytes"] < 10 * 2**20}
    assert {v["name"] for v in manifest["variants"]} == small
