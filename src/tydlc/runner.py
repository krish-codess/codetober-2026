"""One run = discover candidates, sweep every property over generated data, shrink failures.

The sweep never stops at the first failure: each example is judged against every
property, which is what makes per-property failure frequency measurable. Shrinking
is a separate, targeted Hypothesis search per falsified property.
"""

from __future__ import annotations

import csv
import json
import logging
import multiprocessing
import os
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from random import Random
from typing import Any

import duckdb
from hypothesis import HealthCheck, Phase, find, given, settings
from hypothesis import seed as hypothesis_seed
from hypothesis.errors import NoSuchExample

from tydlc.discovery import discover
from tydlc.engine import Engine, make_engine
from tydlc.generators import datasets
from tydlc.ingest import ingest, load_staged
from tydlc.properties import Ctx, Property
from tydlc.schema import Dataset, Schema, total_rows
from tydlc.subjects import Subject, get_subject

log = logging.getLogger("tydlc.runner")

CRASH = Property("pipeline_does_not_crash", "the pipeline executes without a SQL error",
                 lambda c: True)
_OUTCOME = {True: "pass", False: "fail", None: "vacuous"}
_QUIET = list(HealthCheck)


def evaluate(engine: Engine, props: list[Property], ds: Dataset) -> dict[str, bool | None]:
    """Judge one dataset against every property. A raising check counts as a violation."""
    try:
        out = engine.run(ds)
    except Exception:  # any engine error on valid input is the finding
        return {CRASH.name: False, **{p.name: None for p in props}}
    ctx = Ctx(ds, out, engine.execute)
    results: dict[str, bool | None] = {CRASH.name: True}
    for p in props:
        try:
            results[p.name] = p.check(ctx)
        except Exception:
            results[p.name] = False
    return results


def candidates(subject: Subject, engine: Engine, data_dir: Path, *,
               reingest: bool = True) -> list[Property]:
    """Invariant candidates inferred from the pipeline's behaviour on its real sample
    data, which enters through the same validating boundary as any other source."""
    staged = data_dir / subject.name
    if reingest:
        ingest(subject.schema, subject.seed_dir, staged)
    seed = load_staged(subject.schema, staged)
    return discover([Ctx(seed, engine.run(seed), engine.execute)],
                    [m for m, _ in subject.models])


def confidence(passed: int, failed: int) -> float:
    """Rule of three: after n clean non-vacuous examples the true violation rate is below
    3/n with 95% confidence, so 1 - 3/n lower-bounds how often the invariant holds."""
    return 0.0 if failed or not passed else max(0.0, 1 - 3 / passed)


def shrink(subject: Subject, engine: Engine, prop: Property, seed: int,
           max_examples: int) -> tuple[Dataset | None, int, int]:
    """Smallest dataset violating `prop`, with the predicate calls and milliseconds spent."""
    calls = 0
    t = time.perf_counter()

    def fails(ds: Dataset) -> bool:
        nonlocal calls
        calls += 1
        return evaluate(engine, [prop], ds)[prop.name] is False

    minimal: Dataset | None
    try:
        minimal = find(datasets(subject.schema), fails, random=Random(seed),
                       settings=settings(max_examples=max_examples, database=None,
                                         deadline=None, suppress_health_check=_QUIET,
                                         # Phase.explain re-randomises the minimal example
                                         # to annotate it; that was 95% of shrink time.
                                         phases=[Phase.generate, Phase.shrink]))
    except NoSuchExample:
        return None, calls, round((time.perf_counter() - t) * 1000)
    return _drop_rows(subject.schema, minimal, fails), calls, round(
        (time.perf_counter() - t) * 1000)


def _without(schema: Schema, ds: Dataset, table: str, index: int) -> Dataset:
    """`ds` minus one row and, transitively, every row that referenced it."""
    out = {name: list(rows) for name, rows in ds.items()}
    del out[table][index]
    for t in schema:  # parents first, so one sweep cascades all the way down
        for c in t.columns:
            if c.references:
                keys = {r[c.references[1]] for r in out[c.references[0]]}
                out[t.name] = [r for r in out[t.name] if r[c.name] is None or r[c.name] in keys]
    return out


def _drop_rows(schema: Schema, ds: Dataset, fails: Callable[[Dataset], bool]) -> Dataset:
    """Make the result 1-minimal in rows: no single row can be removed and still fail.
    Hypothesis usually gets there alone, but a foreign key is drawn as an index into the
    parent rows, and removing a parent can need a coordinated change it does not find."""
    while True:
        for table, index in [(t.name, i) for t in schema for i in range(len(ds[t.name]))]:
            smaller = _without(schema, ds, table, index)
            if fails(smaller):
                ds = smaller
                break
        else:
            return ds


# Shrink searches are independent, so they fan out over processes. Properties hold
# closures and cannot be pickled; each worker rebuilds them and looks them up by name.
_worker: dict[str, Any] = {}


def _init_worker(subject_name: str, engine_name: str, dsn: str | None, data_dir: Path,
                 discover_invariants: bool) -> None:
    subject = get_subject(subject_name)
    engine = make_engine(engine_name, subject, dsn)
    found = candidates(subject, engine, data_dir, reingest=False) if discover_invariants else []
    _worker.update(subject=subject, engine=engine,
                   props={p.name: p for p in [CRASH, *subject.properties, *found]})


def _shrink_task(name: str, seed: int, max_examples: int) -> tuple[Dataset | None, int, int]:
    return shrink(_worker["subject"], _worker["engine"], _worker["props"][name], seed,
                  max_examples)


def run_suite(subject: Subject, engine_name: str, *, seed: int, max_examples: int,
              data_dir: Path, run_key: str, dsn: str | None = None, workers: int | None = None,
              discover_invariants: bool = True) -> dict[str, Any]:
    t0 = time.perf_counter()
    engine = make_engine(engine_name, subject, dsn)
    discovered = candidates(subject, engine, data_dir) if discover_invariants else []
    props = [*subject.properties, *discovered]
    t_discover = time.perf_counter()

    run_dir = data_dir / "runs" / run_key
    run_dir.mkdir(parents=True, exist_ok=True)
    first_failure: dict[str, Dataset] = {}
    examples = rows = 0
    with open(run_dir / "outcomes.csv", "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["example", "property", "outcome", "input_rows"])

        @hypothesis_seed(seed)
        @settings(max_examples=max_examples, database=None, deadline=None,
                  suppress_health_check=_QUIET, phases=[Phase.generate])
        @given(datasets(subject.schema))
        def sweep(ds: Dataset) -> None:
            nonlocal examples, rows
            n = total_rows(ds)
            for name, result in evaluate(engine, props, ds).items():
                writer.writerow([examples, name, _OUTCOME[result], n])
                if result is False:
                    first_failure.setdefault(name, ds)
            examples += 1
            rows += n

        sweep()
    t_sweep = time.perf_counter()

    # Per-example outcomes land in Parquet sorted by property, so a later scan for one
    # property prunes row groups on min/max; the tallies come from DuckDB, not Python.
    parquet = str(run_dir / "outcomes.parquet").replace("'", "''")
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_csv(?, header=true) ORDER BY property, example) "
                f"TO '{parquet}' (FORMAT parquet, ROW_GROUP_SIZE 122880)",
                [str(run_dir / "outcomes.csv")])
    (run_dir / "outcomes.csv").unlink()
    tallies = {r[0]: r[1:] for r in con.execute(
        "SELECT property, count(*) FILTER (outcome = 'pass'), count(*) FILTER (outcome = 'fail'),"
        " count(*) FILTER (outcome = 'vacuous') FROM read_parquet(?) GROUP BY property",
        [str(run_dir / "outcomes.parquet")]).fetchall()}
    t_aggregate = time.perf_counter()

    falsified = [p for p in [CRASH, *props] if tallies[p.name][1]]
    workers = min(workers or os.cpu_count() or 1, len(falsified))
    shrunk: dict[str, tuple[Dataset | None, int, int]] = {}
    if workers > 1:
        # spawn everywhere: forking a process that holds database connections (or
        # the API's threads) is not safe, and it keeps Linux and Windows identical.
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=_init_worker, initargs=(
                subject.name, engine_name, dsn, data_dir, discover_invariants)) as pool:
            futures = {p.name: pool.submit(_shrink_task, p.name, seed, max_examples)
                       for p in falsified}
            shrunk = {name: f.result() for name, f in futures.items()}
    else:
        shrunk = {p.name: shrink(subject, engine, p, seed, max_examples) for p in falsified}

    known = subject.known_bugs.get(engine.name, {})
    results = []
    for p in [CRASH, *props]:
        passed, failed, vacuous = tallies[p.name]
        entry: dict[str, Any] = {
            "name": p.name, "source": p.source, "description": p.description,
            "passed": passed, "failed": failed, "vacuous": vacuous,
            "status": "falsified" if failed else "held" if passed else "vacuous",
            "confidence": round(confidence(passed, failed), 4),
            "failure_frequency": round(failed / (passed + failed), 4) if passed + failed else None,
            "known_bug": known.get(p.name), "failure": None,
        }
        if failed:
            minimal, calls, ms = shrunk[p.name]
            # find() re-generates from the seed; if it misses a rare failure the sweep
            # saw, fall back to the sweep's own unshrunk counterexample.
            dataset = minimal if minimal is not None else first_failure[p.name]
            entry["failure"] = {
                "minimal_dataset": json.loads(json.dumps(dataset, default=str)),
                "minimal_rows": total_rows(dataset), "shrunk": minimal is not None,
                "shrink_calls": calls, "shrink_ms": ms,
            }
        results.append(entry)
    t_end = time.perf_counter()

    declared = [r for r in results if r["source"] == "declared"]
    found = [r for r in results if r["source"] == "discovered"]
    held = sum(r["status"] == "held" for r in found)
    report = {
        "run_key": run_key, "subject": subject.name, "engine": engine.name, "seed": seed,
        "max_examples": max_examples, "examples": examples, "rows_generated": rows,
        "shrink_workers": workers,
        "timings_ms": {
            "discover": round((t_discover - t0) * 1000),
            "sweep": round((t_sweep - t_discover) * 1000),
            "aggregate": round((t_aggregate - t_sweep) * 1000),
            "shrink": round((t_end - t_aggregate) * 1000),
            "total": round((t_end - t0) * 1000),
        },
        "throughput": {
            "examples_per_s": round(examples / (t_sweep - t_discover), 1),
            "rows_per_s": round(rows / (t_sweep - t_discover), 1),
        },
        "discovery": {"candidates": len(found), "held": held,
                      "falsified": len(found) - held,
                      "hit_rate": round(held / len(found), 4) if found else None},
        # The gate: a declared property may only fail if it is a documented known bug,
        # and a documented known bug must still fail (otherwise the baseline is stale).
        "unexpected_failures": [r["name"] for r in declared
                                if r["status"] == "falsified" and not r["known_bug"]],
        "stale_known_bugs": [r["name"] for r in declared
                             if r["status"] != "falsified" and r["known_bug"]],
        "properties": results,
    }
    report["ok"] = not report["unexpected_failures"] and not report["stale_known_bugs"]
    engine.close()
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    log.info("run finished", extra={k: report[k] for k in (
        "engine", "seed", "examples", "rows_generated", "timings_ms", "discovery",
        "unexpected_failures", "stale_known_bugs", "ok")})
    return report
