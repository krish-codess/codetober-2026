#!/usr/bin/env python3
"""On-device benchmark harness. One file; needs only numpy and a runtime (onnxruntime or openvino).

It ships inside every deployment package and runs on the board itself:

    python3 edge_bench.py --package . --target rpi5 --out results.jsonl

For each model in the package it
  1. checks the file against the sha256 in the manifest (a corrupted copy is refused, not timed),
  2. re-verifies accuracy on the bundled evaluation images and compares every prediction with the
     ones the build host recorded (different CPUs round INT8 differently; this is where it shows),
  3. times single-image inference: warm-up, then N runs, reporting p50/p95/p99,
  4. measures power from a real sensor if the board has one: idle, then under sustained inference,
     and reports energy per inference net of idle.

Nothing is estimated. If there is no sensor, power is null and the reason is recorded.
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import hashlib
import json
import os
import platform
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

SCHEMA = 1

# --- power sensors ----------------------------------------------------------------------------
# Each sensor is a zero-argument callable returning instantaneous watts.


def _battery_windows() -> Tuple[Optional[Callable[[], float]], str]:
    """Whole-system draw from the battery fuel gauge. Only meaningful while discharging."""

    class State(ctypes.Structure):
        _fields_ = [
            ("AcOnLine", ctypes.c_ubyte),
            ("BatteryPresent", ctypes.c_ubyte),
            ("Charging", ctypes.c_ubyte),
            ("Discharging", ctypes.c_ubyte),
            ("Spare", ctypes.c_ubyte * 3),
            ("Tag", ctypes.c_ubyte),
            ("MaxCapacity", ctypes.c_ulong),
            ("RemainingCapacity", ctypes.c_ulong),
            ("Rate", ctypes.c_long),  # mW, negative while discharging
            ("EstimatedTime", ctypes.c_ulong),
            ("DefaultAlert1", ctypes.c_ulong),
            ("DefaultAlert2", ctypes.c_ulong),
        ]

    def state() -> State:
        s = State()
        ctypes.windll.powrprof.CallNtPowerInformation(5, None, 0, ctypes.byref(s), ctypes.sizeof(s))  # type: ignore[attr-defined]
        return s

    s = state()
    if not s.BatteryPresent:
        return None, "no battery"
    if s.AcOnLine or not s.Discharging:
        return None, "on AC power: the battery gauge only measures draw while discharging; unplug and re-run"
    return (lambda: abs(state().Rate) / 1000.0), "battery"


def _rapl() -> Tuple[Optional[Callable[[], float]], str]:
    """Intel/AMD package energy counter. CPU package only; needs read access to energy_uj."""
    base = "/sys/class/powercap/intel-rapl:0"
    try:
        wrap = int(Path(base, "max_energy_range_uj").read_text())
        last = [int(Path(base, "energy_uj").read_text()), time.perf_counter()]
    except (OSError, ValueError) as exc:
        return None, "RAPL not readable (%s)" % type(exc).__name__

    def read() -> float:
        e, t = int(Path(base, "energy_uj").read_text()), time.perf_counter()
        de = e - last[0] if e >= last[0] else e + wrap - last[0]
        dt = max(t - last[1], 1e-6)
        last[0], last[1] = e, t
        return de / 1e6 / dt

    return read, "rapl"


def _hwmon() -> Tuple[Optional[Callable[[], float]], str]:
    """Kernel power monitors (INA219/INA226/INA3221 boards, including Jetson rails): microwatts."""
    paths = sorted(glob.glob("/sys/class/hwmon/hwmon*/power1_input"))
    if not paths:
        return None, "no hwmon power sensor"
    return (lambda: int(Path(paths[0]).read_text()) / 1e6), "hwmon"


def parse_pmic(text: str) -> float:
    """Sum of volts x amps over the rails printed by `vcgencmd pmic_read_adc` (Raspberry Pi 5)."""
    amps: Dict[str, float] = {}
    volts: Dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or "=" not in parts[1]:
            continue
        rail, value = parts[0], float(parts[1].split("=")[1].rstrip("AV"))
        if rail.endswith("_A"):
            amps[rail[:-2]] = value
        elif rail.endswith("_V"):
            volts[rail[:-2]] = value
    return sum(amps[r] * volts[r] for r in amps if r in volts)


def _pmic() -> Tuple[Optional[Callable[[], float]], str]:
    def read() -> float:
        return parse_pmic(
            subprocess.run(["vcgencmd", "pmic_read_adc"], capture_output=True, text=True, timeout=5).stdout
        )

    try:
        if read() <= 0:
            return None, "vcgencmd returned no rails"
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, "vcgencmd pmic_read_adc not available (not a Pi 5?)"
    return read, "pmic"


def _cmd(command: str) -> Tuple[Optional[Callable[[], float]], str]:
    """Any external meter: a command that prints watts (USB power meter, lab supply, smart plug)."""

    def read() -> float:
        out = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10).stdout
        return float(out.strip().split()[0])

    try:
        read()
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return None, "power command failed: %s" % exc
    return read, "cmd"


def detect_power(choice: str) -> Tuple[Optional[Callable[[], float]], str]:
    """Returns (sensor, source) or (None, reason)."""
    if choice == "none":
        return None, "disabled with --power none"
    if choice.startswith("cmd:"):
        return _cmd(choice[4:])
    probes: Dict[str, Callable[[], Tuple[Optional[Callable[[], float]], str]]] = {
        "pmic": _pmic,
        "hwmon": _hwmon,
        "rapl": _rapl,
    }
    if sys.platform == "win32":
        probes = {"battery": _battery_windows}
    if choice != "auto":
        return (
            probes[choice]()
            if choice in probes
            else (None, "sensor %r does not exist on this platform" % choice)
        )
    reasons = []
    for probe in probes.values():
        sensor, note = probe()
        if sensor:
            return sensor, note
        reasons.append(note)
    return None, "; ".join(reasons)


class Sampler:
    """Reads the sensor on a background thread while the main thread idles or runs inference."""

    def __init__(self, sensor: Callable[[], float], interval: float, scale: float, offset: float) -> None:
        self.sensor, self.interval, self.scale, self.offset = sensor, interval, scale, offset

    def during(self, work: Callable[[], None]) -> List[float]:
        samples: List[float] = []
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(self.interval):
                try:
                    samples.append(self.sensor() * self.scale + self.offset)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass  # one failed read is a missing sample, not a failed benchmark

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        try:
            work()
        finally:
            stop.set()
            thread.join()
        return samples


# --- runtimes ---------------------------------------------------------------------------------


def make_runner(
    runtime: str, path: Path, threads: int, providers: List[str]
) -> Tuple[Callable[[np.ndarray], np.ndarray], str, str]:
    """Returns (infer, runtime_version, provider). infer maps 1x3xHxW float32 to logits."""
    if runtime == "onnxruntime":
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.log_severity_level = 3
        sess = ort.InferenceSession(str(path), opts, providers=providers or ["CPUExecutionProvider"])
        name = sess.get_inputs()[0].name
        return (lambda x: sess.run(None, {name: x})[0]), ort.__version__, sess.get_providers()[0]
    if runtime == "openvino":
        import openvino as ov

        model = ov.Core().read_model(str(path))
        shape = list(model.inputs[0].partial_shape.get_max_shape())
        shape[0] = 1
        model.reshape(shape)
        config = {"PERFORMANCE_HINT": "LATENCY"}
        if threads:
            config["INFERENCE_NUM_THREADS"] = str(threads)
        compiled = ov.compile_model(model, "CPU", config)
        return (lambda x: compiled(x)[0]), ov.__version__.split("-")[0], "CPU"
    raise SystemExit("unknown runtime %r (use onnxruntime or openvino)" % runtime)


# --- measurement ------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def device_info() -> Dict[str, Any]:
    cpu = platform.processor()
    board = None
    try:
        board = Path("/proc/device-tree/model").read_text().strip("\x00\n ")
    except OSError:
        pass
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith(("model name", "hardware")):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    return {
        "machine": machine,
        "cpu_model": cpu or "unknown",
        "cores": os.cpu_count() or 1,
        "os": platform.platform(),
        "board": board,
    }


def percentiles(ms: np.ndarray) -> Dict[str, Any]:
    p50, p95, p99 = np.percentile(ms, [50, 95, 99])
    return {
        "n": len(ms),
        "p50": float(p50),
        "p95": float(p95),
        "p99": float(p99),
        "mean": float(ms.mean()),
        "min": float(ms.min()),
    }


def bench_variant(
    variant: Dict[str, Any],
    pkg: Path,
    images: np.ndarray,
    labels: np.ndarray,
    args: argparse.Namespace,
    sampler: Optional[Sampler],
) -> Dict[str, Any]:
    path = pkg / variant["file"]
    digest = sha256(path)
    if digest != variant["sha256"]:
        raise ValueError(
            "%s does not match the manifest sha256; the copy is corrupt, re-deploy the package"
            % variant["file"]
        )
    infer, version, provider = make_runner(args.runtime, path, args.threads, args.providers)
    inputs = [np.ascontiguousarray(img.transpose(2, 0, 1)[None], dtype=np.float32) for img in images]

    preds = np.array([int(infer(x).argmax()) for x in inputs])
    accuracy = {
        "n": len(labels),
        "top1": float((preds == labels).mean()),
        "agree_host": float((preds == np.array(variant["expected"])).mean()),
    }

    for i in range(args.warmup):
        infer(inputs[i % len(inputs)])
    times = np.empty(args.runs)
    for i in range(args.runs):
        x = inputs[i % len(inputs)]
        start = time.perf_counter_ns()
        infer(x)
        times[i] = (time.perf_counter_ns() - start) / 1e6
    latency = percentiles(times)

    power = None
    if sampler:
        idle = sampler.during(lambda: time.sleep(args.power_seconds))
        count = [0]

        def load() -> None:
            end = time.perf_counter() + args.power_seconds
            while time.perf_counter() < end:
                infer(inputs[count[0] % len(inputs)])
                count[0] += 1

        busy = sampler.during(load)
        if idle and busy:
            idle_w, load_w = float(np.median(idle)), float(np.median(busy))
            per_inference_s = args.power_seconds / max(count[0], 1)
            power = {
                "idle_w": idle_w,
                "load_w": load_w,
                # Joules = watts x seconds. Net of idle: what running this model adds to the bill.
                "energy_mj": max(load_w - idle_w, 0.0) * per_inference_s * 1000,
                "energy_gross_mj": load_w * per_inference_s * 1000,
                "samples": len(busy),
                "unit": "W",
            }
    return {
        "variant": variant["name"],
        "model_sha256": digest,
        "runtime": args.runtime,
        "runtime_version": version,
        "provider": provider,
        "threads": args.threads,
        "latency_ms": latency,
        "accuracy": accuracy,
        "power": power,
    }


def post(url: str, token: str, body: Dict[str, Any], tries: int = 5) -> str:
    """Upload one result. Safe to repeat: the server deduplicates on result_id. Retries only what
    can succeed later (network errors, 429, 5xx), with capped exponential backoff and jitter."""
    data = json.dumps(body).encode()
    for attempt in range(tries):
        request = urllib.request.Request(  # noqa: S310
            url, data=data, headers={"content-type": "application/json", "authorization": "Bearer " + token}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                return "uploaded (%d)" % response.status
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                return "rejected (%d): %s" % (exc.code, exc.read().decode(errors="replace")[:300])
            reason = "HTTP %d" % exc.code
        except (urllib.error.URLError, OSError) as exc:
            reason = str(exc)
        if attempt < tries - 1:
            time.sleep(min(2.0**attempt, 16.0) + random.random())  # noqa: S311
    return "not uploaded after %d attempts (%s); the result is in the output file, upload it later" % (
        tries,
        reason,
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--package", type=Path, default=Path(__file__).resolve().parent, help="unpacked package directory"
    )
    ap.add_argument("--target", help="target name to record (default: the package's target)")
    ap.add_argument("--runtime", default="onnxruntime", choices=["onnxruntime", "openvino"])
    ap.add_argument(
        "--providers", type=lambda s: s.split(","), default=[], help="onnxruntime providers, in order"
    )
    ap.add_argument("--threads", type=int, default=0, help="0 = runtime default")
    ap.add_argument("--variants", type=lambda s: s.split(","), help="subset of variant names")
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument(
        "--power", default="auto", help="auto | none | battery | rapl | hwmon | pmic | cmd:<prints watts>"
    )
    ap.add_argument(
        "--power-seconds", type=float, default=20.0, help="length of the idle and the loaded window"
    )
    ap.add_argument("--power-interval", type=float, default=0.5)
    # Calibration against a reference meter: reported = raw * scale + offset.
    ap.add_argument("--power-scale", type=float, default=float(os.environ.get("EDGE_POWER_SCALE", "1")))
    ap.add_argument("--power-offset-w", type=float, default=float(os.environ.get("EDGE_POWER_OFFSET_W", "0")))
    ap.add_argument("--out", type=Path, default=Path("results.jsonl"))
    ap.add_argument("--post", help="API base URL to upload to; token from $SQUEEZE_DEVICE_TOKEN")
    args = ap.parse_args(argv)

    manifest = json.loads((args.package / "manifest.json").read_text())
    images = np.load(args.package / manifest["eval_pack"]["images"])
    labels = np.load(args.package / manifest["eval_pack"]["labels"])
    target = args.target or manifest["target"]["name"]
    budget = manifest["target"].get("budget_p95_ms")
    sensor, note = detect_power(args.power)
    sampler = Sampler(sensor, args.power_interval, args.power_scale, args.power_offset_w) if sensor else None
    device = device_info()
    print(
        "target %s on %s (%s), power: %s" % (target, device["cpu_model"], device["machine"], note),
        file=sys.stderr,
    )

    status = 0
    for variant in manifest["variants"]:
        if args.variants and variant["name"] not in args.variants:
            continue
        try:
            result = bench_variant(variant, args.package, images, labels, args, sampler)
        except Exception as exc:  # one broken model must not hide the results of the others
            print("%-28s FAILED: %s" % (variant["name"], exc), file=sys.stderr)
            status = 1
            continue
        if result["power"]:
            result["power"]["source"] = note
            result["power"]["scale"], result["power"]["offset_w"] = args.power_scale, args.power_offset_w
        else:
            result["power_unavailable"] = note
        result.update(
            schema=SCHEMA,
            target=target,
            run_id=manifest["run_id"],
            device=device,
            measured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        result["result_id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:32]
        with args.out.open("a") as f:
            f.write(json.dumps(result, sort_keys=True) + "\n")
        lat, pw = result["latency_ms"], result["power"]
        print(
            "%-28s top1 %.3f agree %.3f | p50 %7.2f p95 %7.2f p99 %7.2f ms%s | %s"
            % (
                variant["name"],
                result["accuracy"]["top1"],
                result["accuracy"]["agree_host"],
                lat["p50"],
                lat["p95"],
                lat["p99"],
                ""
                if budget is None
                else (" (budget %.0f: %s)" % (budget, "ok" if lat["p95"] <= budget else "OVER")),
                "%.2f W, %.1f mJ/inference" % (pw["load_w"], pw["energy_mj"]) if pw else "no power",
            ),
            file=sys.stderr,
        )
        if args.post:
            token = os.environ.get("SQUEEZE_DEVICE_TOKEN", "")
            print("  " + post(args.post.rstrip("/") + "/v1/results", token, result), file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
