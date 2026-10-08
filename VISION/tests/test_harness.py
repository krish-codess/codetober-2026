"""The on-device harness, run exactly as it ships: from an unpacked package, against a real API
process over HTTP. This is the backend half of the primary journey: build -> package -> benchmark
on the device -> upload -> appears on the tradeoff curve."""

import importlib.util
import json
import socket
import sys
import tarfile
import threading
import time
import urllib.error

import pytest

pytest.importorskip("torch")
import uvicorn

from squeeze import config, db, package, regress
from squeeze.api import create_app
from squeeze.schemas import BenchResult

pytestmark = pytest.mark.ml


@pytest.fixture(scope="module")
def unpacked(smoke_run, tmp_path_factory):
    root, ds, record = smoke_run
    out = tmp_path_factory.mktemp("pkg")
    built = package.build(root / "runs" / record["run_id"], ds, config.targets()["ci"], out)
    with tarfile.open(out / built["filename"]) as tar:
        tar.extractall(out / "x", filter="data")
    pkg = out / "x" / built["manifest"]["name"]
    spec = importlib.util.spec_from_file_location("edge_bench_shipped", pkg / "edge_bench.py")
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    return pkg, harness, record, out / f"{built['manifest']['name']}.package.json"


@pytest.fixture
def server(cfg, monkeypatch):
    """A real uvicorn process boundary: sockets, HTTP parsing, the lot."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=port, log_config=None))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.02)
    monkeypatch.setenv("SQUEEZE_DEVICE_TOKEN", cfg.device_token)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


def run(harness, pkg, out, *extra):
    args = ["--package", str(pkg), "--out", str(out), "--runs", "8", "--warmup", "2", *extra]
    code = harness.main(args)
    return code, [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []


def test_harness_results_flow_to_the_tradeoff_curve(unpacked, server, cfg, tmp_path):
    pkg, harness, record, package_json = unpacked
    con = db.connect(cfg.db_path)
    db.ingest_pipeline(con, record)
    db.ingest_package(con, json.loads(package_json.read_text()))

    code, results = run(harness, pkg, tmp_path / "r.jsonl", "--power", "none", "--post", server)
    assert code == 0
    shipped = {v["name"] for v in json.loads((pkg / "manifest.json").read_text())["variants"]}
    assert {r["variant"] for r in results} == shipped and shipped
    for r in results:
        BenchResult.model_validate(r)  # the harness and the server agree on the contract
        assert r["accuracy"]["agree_host"] == 1.0  # same machine as the build host: identical predictions
        assert r["latency_ms"]["p50"] <= r["latency_ms"]["p95"] <= r["latency_ms"]["p99"]
        assert r["power"] is None and r["power_unavailable"]  # nothing invented when there is no sensor

    import urllib.request

    with urllib.request.urlopen(f"{server}/v1/tradeoff?target=ci") as response:
        points = {p["name"]: p for p in json.load(response)["points"]}
    for name in shipped:
        assert points[name]["bench"]["runs"] == 1 and points[name]["bench"]["energy_mj"] is None
    assert points["teacher-r50"]["bench"] is None  # too large for the ci package: honestly unmeasured

    # the same file uploaded again (a device that lost its connection and retried) changes nothing
    assert db.ingest_dir(con, tmp_path, set(config.targets())) == {"duplicate": len(results)}

    # and the regression gate reads the same file
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"reference": "student-kd", "latency_ratio": {}}))
    assert regress.run(tmp_path / "r.jsonl", baseline, 0.3, update=True) == 0
    assert regress.run(tmp_path / "r.jsonl", baseline, 5.0, update=False) == 0


def test_power_is_measured_from_the_sensor_and_net_of_idle(unpacked, tmp_path):
    pkg, harness, _, _ = unpacked
    meter = tmp_path / "meter.py"
    meter.write_text("print(5.0)")
    sensor = f"cmd:{sys.executable} {meter}"
    code, results = run(
        harness, pkg, tmp_path / "p.jsonl", "--variants", "student-kd", "--power", sensor,
        "--power-seconds", "1.5", "--power-interval", "0.2", "--power-scale", "2", "--power-offset-w", "0.5",
    )  # fmt: skip
    assert code == 0 and len(results) == 1
    power = results[0]["power"]
    assert power["source"] == "cmd" and power["idle_w"] == power["load_w"] == 10.5  # 5.0 * 2 + 0.5
    assert power["energy_mj"] == 0.0 and power["energy_gross_mj"] > 0 and power["samples"] >= 2
    BenchResult.model_validate(results[0])


def test_a_corrupted_model_is_refused_not_benchmarked(unpacked, tmp_path):
    pkg, harness, _, _ = unpacked
    model = pkg / "models" / "student-kd.onnx"
    original = model.read_bytes()
    try:
        model.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        code, results = run(
            harness, pkg, tmp_path / "c.jsonl", "--power", "none", "--variants", "student-kd,student-ce"
        )
    finally:
        model.write_bytes(original)
    assert code == 1 and [r["variant"] for r in results] == ["student-ce"]  # the healthy model still ran


def test_pi5_pmic_output_is_summed_per_rail(unpacked):
    text = (
        " 3V7_WL_SW_A current(0)=0.00390372A\n 3V3_SYS_A current(1)=0.05490858A\n VDD_CORE_A current(7)=1.50000000A\n"
        " 3V7_WL_SW_V volt(8)=3.70000000V\n 3V3_SYS_V volt(9)=3.30000000V\n VDD_CORE_V volt(15)=0.80000000V\n"
        " EXT5V_V volt(24)=5.10000000V\n"
    )
    assert unpacked[1].parse_pmic(text) == pytest.approx(0.00390372 * 3.7 + 0.05490858 * 3.3 + 1.5 * 0.8)


def test_upload_retries_with_backoff_then_gives_up_understandably(unpacked, monkeypatch):
    harness = unpacked[1]
    sleeps, calls = [], []
    monkeypatch.setattr(harness.time, "sleep", sleeps.append)

    class Ok:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def flaky(request, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("connection refused")
        return Ok()

    monkeypatch.setattr(harness.urllib.request, "urlopen", flaky)
    assert harness.post("http://x/v1/results", "t", {}) == "uploaded (201)"
    assert len(calls) == 3 and len(sleeps) == 2 and sleeps[0] < sleeps[1] < 4  # exponential, with jitter

    def down(request, timeout):
        raise urllib.error.URLError("no route to host")

    sleeps.clear()
    monkeypatch.setattr(harness.urllib.request, "urlopen", down)
    message = harness.post("http://x/v1/results", "t", {}, tries=4)
    assert message.startswith("not uploaded after 4 attempts") and "upload it later" in message
    assert len(sleeps) == 3 and max(sleeps) <= 17  # capped

    def rejected(request, timeout):
        import io

        raise urllib.error.HTTPError("u", 422, "bad", {}, io.BytesIO(b'{"error": "nope"}'))

    sleeps.clear()
    monkeypatch.setattr(harness.urllib.request, "urlopen", rejected)
    assert harness.post("http://x/v1/results", "t", {}).startswith("rejected (422)") and not sleeps  # no point retrying
