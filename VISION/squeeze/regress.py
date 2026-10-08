"""Benchmark regression gate for CI.

Shared CI runners differ from run to run, so absolute milliseconds cannot be gated. Two things can:
  * accuracy on the bundled evaluation pack, which is deterministic and must not move, and
  * each model's latency relative to a reference model measured in the same job on the same
    machine. That ratio is compared with the committed baseline for the same CPU model.
A CPU with no baseline yet gets the accuracy gate only, and its ratios are printed so they can be
committed with --update.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

ACCURACY_TOL = 0.02  # two images of the 100-image pack: INT8 rounding differs slightly across CPUs
AGREE_MIN = 0.97


def summarise(results: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Median over repeated runs of each variant."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        groups.setdefault(r["variant"], []).append(r)
    return {
        name: {
            "top1": statistics.median(r["accuracy"]["top1"] for r in rows),
            "agree_host": statistics.median(r["accuracy"]["agree_host"] for r in rows),
            "p50_ms": statistics.median(r["latency_ms"]["p50"] for r in rows),
        }
        for name, rows in groups.items()
    }


def check(
    current: dict[str, dict[str, float]], baseline: dict[str, Any], cpu: str, tolerance: float
) -> tuple[list[str], list[str]]:
    """Returns (failures, notes). An empty failure list means the gate passes."""
    failures: list[str] = []
    notes: list[str] = []
    ref = baseline["reference"]
    for name, want in baseline["accuracy"].items():
        got = current.get(name)
        if got is None:
            failures.append(f"{name}: in the baseline but not benchmarked")
            continue
        if got["top1"] < want - ACCURACY_TOL:
            failures.append(f"{name}: top-1 {got['top1']:.3f} fell below baseline {want:.3f} - {ACCURACY_TOL}")
        if got["agree_host"] < AGREE_MIN:
            failures.append(f"{name}: only {got['agree_host']:.3f} of predictions match the build host")
    if ref not in current:
        return [*failures, f"reference model {ref} was not benchmarked"], notes
    ratios = {name: got["p50_ms"] / current[ref]["p50_ms"] for name, got in current.items() if name != ref}
    known = baseline["latency_ratio"].get(cpu)
    if known is None:
        notes.append(f"no latency baseline for CPU {cpu!r}: latency not gated. Ratios vs {ref}: {ratios}")
        return failures, notes
    for name, want in known.items():
        got_ratio = ratios.get(name)
        if got_ratio is not None and got_ratio > want * (1 + tolerance):
            failures.append(
                f"{name}: {got_ratio:.2f}x the latency of {ref}, baseline {want:.2f}x (+{tolerance:.0%} allowed)"
            )
    return failures, notes


def run(results_path: Path, baseline_path: Path, tolerance: float, update: bool) -> int:
    results = [json.loads(line) for line in results_path.read_text().splitlines() if line.strip()]
    if not results:
        print("no results to check")
        return 1
    cpu = results[0]["device"]["cpu_model"]
    current = summarise(results)
    if update:
        baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {"latency_ratio": {}}
        ref = baseline.get("reference", "student-kd")
        baseline["reference"] = ref
        baseline["accuracy"] = {name: got["top1"] for name, got in sorted(current.items())}
        baseline["latency_ratio"][cpu] = {
            name: round(got["p50_ms"] / current[ref]["p50_ms"], 3)
            for name, got in sorted(current.items())
            if name != ref
        }
        baseline_path.write_text(json.dumps(baseline, indent=1, sort_keys=True) + "\n")
        print(f"baseline updated for {cpu}")
        return 0
    failures, notes = check(current, json.loads(baseline_path.read_text()), cpu, tolerance)
    for name, got in sorted(current.items()):
        print(f"{name:26} top1 {got['top1']:.3f} agree {got['agree_host']:.3f} p50 {got['p50_ms']:.2f} ms")
    for note in notes:
        print("note:", note)
    for failure in failures:
        print("REGRESSION:", failure)
    return 1 if failures else 0
