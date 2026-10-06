"""Materialise every variant, run the workload against each, and run the column-type lab.

Methodology for one cell (variant x query):

  cold     evict the file from the OS page cache, open a fresh DuckDB, time one execution
  counted  one execution through CountingFS: bytes and read calls. Never timed, because the
           counter routes I/O through Python; it also primes the page cache for what follows
  warm     one connection, LAKE_WARMUPS discarded executions, then timed executions

A timed execution covers binding the source, running the query and fetching the result.
Every result is compared with the answer computed from clean/source.parquet.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import numbers
import os
import platform
import re
import tempfile
import time
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from .config import Settings, log, read_json, write_json
from .data import DataError, ident, load_clean
from .formats import (
    LAB_MATRIX,
    MATRIX,
    CountingFS,
    Variant,
    anatomy,
    close_source,
    evict,
    eviction_check,
    open_source,
    shuffled,
    write_variant,
)


def select(only: str | None) -> list[Variant]:
    chosen = [v for v in MATRIX if not only or re.search(only, v.id)]
    if not chosen:
        raise DataError(f"--only {only!r} matches no variant. Ids: {', '.join(v.id for v in MATRIX)}")
    return chosen


def variant_path(s: Settings, v: Variant) -> Path:
    return s.data_dir / "variants" / v.id / v.filename


def checksum(con: duckdb.DuckDBPyConnection, schema: list[list[str]]) -> tuple[int, int]:
    """Order-independent fingerprint of relation `t`, with every column cast to the contract type."""
    cols = ", ".join(f"CAST({ident(n)} AS {t})" for n, t in schema)
    row = con.execute(f"SELECT count(*), bit_xor(hash({cols})) FROM t").fetchone()  # noqa: S608
    assert row is not None
    return int(row[0]), int(row[1] or 0)


# ---------------------------------------------------------------- materialize

def materialize(s: Settings, info: dict[str, Any], only: str | None = None) -> None:
    """Write the clean dataset as every variant and prove each one holds the same rows."""
    table, meta = load_clean(s)
    schema = meta["schema"]
    con = duckdb.connect(config={"threads": s.threads})
    con.register("t", table)
    expected = checksum(con, schema)
    manifest_path = s.data_dir / "variants" / "manifest.json"
    manifest = read_json(manifest_path, {})
    written = 0
    for v in select(only):
        path = variant_path(s, v)
        if manifest.get(v.id, {}).get("source") == meta["fingerprint"] and path.exists():
            continue
        src = shuffled(table, s.seed) if v.layout == "shuffled" else table
        t0, c0 = time.perf_counter(), time.process_time()
        write_variant(src, v, path)
        write_s, write_cpu_s = time.perf_counter() - t0, time.process_time() - c0
        check = duckdb.connect(config={"threads": s.threads})
        open_source(check, v, path, schema)
        got = checksum(check, schema)
        check.close()
        if got != expected:
            path.unlink()
            raise DataError(f"{v.id} does not hold the source data: (rows, checksum) {got} != {expected}. "
                            "The file was deleted; this variant would have benchmarked different data.")
        manifest[v.id] = {**v.describe(), "bytes": path.stat().st_size, "rows": got[0],
                          "write_s": round(write_s, 3), "write_cpu_s": round(write_cpu_s, 3),
                          "source": meta["fingerprint"], **anatomy(v, path)}
        write_json(manifest_path, manifest)  # after every variant, so an interrupted run resumes
        written += 1
        log("materialize.variant", variant=v.id, bytes=manifest[v.id]["bytes"], write_s=round(write_s, 2))
    info.update(rows=table.num_rows * written, variants=written, skipped=written == 0,
                bytes=sum(m["bytes"] for m in manifest.values()))


# ---------------------------------------------------------------- bench

def same(a: list[tuple[Any, ...]], b: list[tuple[Any, ...]]) -> bool:
    """Result equality across readers: numbers within 1e-9 relative (parallel float sums), rest exact."""
    def eq(p: Any, q: Any) -> bool:
        if isinstance(p, numbers.Number) and isinstance(q, numbers.Number):
            return math.isclose(float(p), float(q), rel_tol=1e-9, abs_tol=1e-9)  # type: ignore[arg-type]
        return bool(p == q)
    return len(a) == len(b) and all(len(x) == len(y) and all(map(eq, x, y)) for x, y in zip(a, b, strict=True))


def repeat(fn: Callable[[], Any], most: int, least: int, budget_s: float) -> None:
    """Call fn up to `most` times; after `least`, stop once the cell has spent its budget."""
    t0, n = time.perf_counter(), 0
    while n < most and (n < least or time.perf_counter() - t0 < budget_s):
        fn()
        n += 1


def query_params(sql: str, window: dict[str, Any]) -> dict[str, Any]:
    return {k: window[k] for k in ("lo", "hi") if f"${k}" in sql}


def execute(con: duckdb.DuckDBPyConnection, v: Variant, path: Path, schema: list[list[str]], sql: str,
            params: dict[str, Any], fs: CountingFS | None = None) -> tuple[float, float, list[tuple[Any, ...]]]:
    t0, c0 = time.perf_counter(), time.process_time()
    open_source(con, v, path, schema, fs)
    try:
        rows = con.execute(sql, params).fetchall()
        return time.perf_counter() - t0, time.process_time() - c0, rows
    finally:
        close_source(con)


def environment(s: Settings) -> dict[str, Any]:
    try:
        ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        kb = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kb))  # type: ignore[attr-defined]
        ram = kb.value * 1024
    return {"os": platform.platform(), "machine": platform.machine(), "cpu": platform.processor() or "unknown",
            "logical_cpus": os.cpu_count(), "ram_gb": round(ram / 1e9, 1), "python": platform.python_version(),
            "in_container": Path("/.dockerenv").exists(), "threads": s.threads,
            "versions": {p: metadata.version(p) for p in ("duckdb", "pyarrow", "polars", "fsspec", "numpy")}}


def bench(s: Settings, ds: dict[str, Any], info: dict[str, Any], only: str | None = None) -> int:
    """Run the workload over every materialised variant. Returns the number of failed cells.

    Appends one JSON line per execution, so an interrupted run resumes where it stopped and
    a failed cell is retried on the next run without repeating the cells that worked.
    """
    _, meta = load_clean(s)
    schema, fp = meta["schema"], meta["fingerprint"]
    manifest = read_json(s.data_dir / "variants" / "manifest.json", {})
    variants = [v for v in select(only) if manifest.get(v.id, {}).get("source") == fp]
    if not variants:
        raise DataError("No materialised variants for the current source. Run `bakeoff materialize` first.")
    pa.set_cpu_count(s.threads)
    out = s.data_dir / "results" / "measurements.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        with out.open(encoding="utf-8") as f:
            done = {(r["variant"], r["query"]) for r in map(json.loads, f) if r["kind"] == "warm" and r["source"] == fp}

    ref_con = duckdb.connect(config={"threads": s.threads})
    ref_con.read_parquet((s.data_dir / "clean" / "source.parquet").as_posix()).create_view("t")
    reference = {q["id"]: ref_con.execute(q["sql"], query_params(q["sql"], meta["window"])).fetchall()
                 for q in ds["queries"]}
    biggest = max(variants, key=lambda v: manifest[v.id]["bytes"])
    env = {**environment(s), "eviction": eviction_check(variant_path(s, biggest)),
           "settings": {"cold_runs": s.cold_runs, "warmups": s.warmups, "warm_runs": s.warm_runs,
                        "cell_budget_s": s.cell_budget_s}}
    write_json(s.data_dir / "results" / "env.json", env)
    log("bench.env", **env)

    failed = executions = 0
    with out.open("a", encoding="utf-8") as sink:
        def record(v: Variant, q: dict[str, Any], kind: str, **fields: Any) -> None:
            nonlocal executions
            executions += 1
            sink.write(json.dumps({"variant": v.id, "query": q["id"], "kind": kind, "source": fp,
                                   "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}) + "\n")
            sink.flush()

        for v in variants:
            path = variant_path(s, v)
            for q in ds["queries"]:
                if (v.id, q["id"]) in done:
                    continue
                try:
                    run_cell(s, v, path, schema, q["sql"], query_params(q["sql"], meta["window"]),
                             reference[q["id"]], lambda kind, v=v, q=q, **fields: record(v, q, kind, **fields))
                except Exception as e:  # one broken variant must not cost the other 22
                    failed += 1
                    record(v, q, "error", error=f"{type(e).__name__}: {e}"[:500])
                    log("bench.cell_failed", logging.ERROR, variant=v.id, query=q["id"], error=str(e)[:300])
            log("bench.variant", variant=v.id)
    info.update(rows=executions, failed_cells=failed)
    return failed


def run_cell(s: Settings, v: Variant, path: Path, schema: list[list[str]], sql: str, params: dict[str, Any],
             expect: list[tuple[Any, ...]], record: Callable[..., None]) -> None:
    """Cold runs, one counted run, then warm runs for a single (variant, query)."""
    def connect() -> duckdb.DuckDBPyConnection:
        return duckdb.connect(config={"threads": s.threads})

    def cold() -> None:
        evict(path)
        con = connect()
        sec, cpu, rows = execute(con, v, path, schema, sql, params)
        con.close()
        record("cold", seconds=sec, cpu_s=cpu, ok=same(rows, expect))
    repeat(cold, s.cold_runs, 1, s.cell_budget_s)

    fs, con = CountingFS(), connect()
    _, _, rows = execute(con, v, path, schema, sql, params, fs)
    con.close()
    record("counted", bytes_read=fs.bytes, reads=fs.reads, ok=same(rows, expect))

    con = connect()
    for _ in range(s.warmups):
        execute(con, v, path, schema, sql, params)

    def warm() -> None:
        sec, cpu, rows = execute(con, v, path, schema, sql, params)
        record("warm", seconds=sec, cpu_s=cpu, ok=same(rows, expect))
    repeat(warm, s.warm_runs, 2, s.cell_budget_s)
    con.close()


# ---------------------------------------------------------------- column lab

def lab_columns(n: int, seed: int) -> list[tuple[str, str, pa.Array]]:
    """Synthetic columns with controlled type, cardinality, order and null density."""
    rng = np.random.default_rng(seed)
    base = np.datetime64("2024-01-01", "us")
    cols: list[tuple[str, str, pa.Array]] = [
        ("int_sequential", "int64", pa.array(np.arange(n))),
        ("int_low_card", "int64", pa.array(rng.integers(0, 8, n))),
        ("int_low_card_sorted", "int64", pa.array(np.sort(rng.integers(0, 8, n)))),
        ("int_mid_card", "int64", pa.array(rng.integers(0, 10_000, n))),
        ("int_random", "int64", pa.array(rng.integers(0, 2**62, n))),
        ("float_prices", "double", pa.array(np.round(rng.lognormal(2.5, 0.8, n), 2))),
        ("float_random", "double", pa.array(rng.normal(0, 1, n))),
        ("ts_sorted", "timestamp", pa.array(base + np.cumsum(rng.integers(0, 3_000_000, n)).astype("timedelta64[us]"))),
        ("ts_random", "timestamp",
         pa.array(base + (rng.integers(0, 90 * 86400, n) * 1_000_000).astype("timedelta64[us]"))),
        ("str_low_card", "string", pa.array(np.array(["card", "cash", "dispute", "no_charge", "unknown", "void",
                                                      "flex", "voucher"])[rng.integers(0, 8, n)])),
        ("str_mid_card", "string", pa.array(np.char.add("user_", rng.integers(0, 10_000, n).astype(str)))),
        ("str_unique", "string", pa.array([rng.bytes(16).hex() for _ in range(n)])),
        ("bool_skewed", "bool", pa.array(rng.random(n) < 0.1)),
    ]
    for name, typ, arr in [c for c in cols if c[0] in ("int_mid_card", "float_random", "str_mid_card")]:
        for pct in (10, 50, 90):
            mask = pa.array(rng.random(n) < pct / 100)
            nulled = pc.if_else(mask, pa.nulls(n, arr.type), arr)  # type: ignore[attr-defined]
            cols.append((f"{name}_null{pct}", typ, nulled))
    return cols


def column_lab(s: Settings, info: dict[str, Any]) -> None:
    """One file per (column, format, codec): how compression depends on type and cardinality."""
    out = s.data_dir / "results" / "columns.json"
    fp = {"rows": s.lab_rows, "seed": s.seed, "variants": [v.id for v in LAB_MATRIX]}
    if read_json(out, {}).get("fingerprint") == fp:
        info.update(skipped=True, rows=0)
        return
    columns, cells = [], []
    with tempfile.TemporaryDirectory(dir=s.data_dir) as tmp:
        for name, typ, arr in lab_columns(s.lab_rows, s.seed):
            table = pa.table({name: arr})
            raw = table.nbytes
            distinct = pc.count_distinct(arr).as_py()  # type: ignore[attr-defined]
            columns.append({"column": name, "type": typ, "distinct": distinct,
                            "null_frac": round(arr.null_count / len(arr), 3), "raw_bytes": raw})
            for v in LAB_MATRIX:
                path = Path(tmp) / v.filename
                write_variant(table, v, path)
                size = path.stat().st_size
                cell = {"column": name, "variant": v.id, "format": v.format, "codec": v.codec, "bytes": size,
                        "ratio": round(raw / size, 3), "bits_per_value": round(size * 8 / len(arr), 3)}
                if v.format == "parquet":
                    cell["encodings"] = anatomy(v, path)["columns"][0]["encodings"]
                cells.append(cell)
                path.unlink()
    write_json(out, {"fingerprint": fp, "rows": s.lab_rows, "columns": columns, "cells": cells})
    info.update(rows=s.lab_rows * len(cells), files=len(cells))
