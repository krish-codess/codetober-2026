"""Turn measurements into results.json, and results into dollars and a recommendation."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, Field

from .config import Settings, load_toml, read_json, write_json
from .data import DataError
from .formats import MATRIX

SCHEMA_VERSION = 1
_COLUMNS = ("{variant: 'VARCHAR', query: 'VARCHAR', kind: 'VARCHAR', source: 'VARCHAR', ts: 'VARCHAR', "
            "seconds: 'DOUBLE', cpu_s: 'DOUBLE', bytes_read: 'BIGINT', reads: 'BIGINT', ok: 'BOOLEAN', "
            "error: 'VARCHAR'}")
_MEASUREMENTS = f"read_json(?, format = 'newline_delimited', columns = {_COLUMNS})"


# ---------------------------------------------------------------- aggregate

def build(s: Settings, ds: dict[str, Any]) -> dict[str, Any]:
    """Aggregate every execution of the current source into the document the API and UI serve."""
    res = s.data_dir / "results"
    meta = read_json(s.data_dir / "clean" / "ingest.json")
    manifest = read_json(s.data_dir / "variants" / "manifest.json", {})
    jsonl = res / "measurements.jsonl"
    if meta is None or not jsonl.exists():
        raise DataError("Nothing to report. Run `bakeoff bench` first.")
    fp = meta["fingerprint"]
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE m AS SELECT row_number() OVER (ORDER BY ts, variant, query, kind, seconds) AS seq, "  # noqa: S608
                f"* EXCLUDE (source) FROM {_MEASUREMENTS} WHERE source = ?", [str(jsonl), fp])
    con.table("m").order("seq").write_parquet(str(res / "measurements.parquet"), compression="zstd")
    agg = con.execute("""
        SELECT variant, query, kind, count(*), median(seconds), min(seconds), max(seconds), avg(seconds),
               coalesce(stddev_samp(seconds), 0), median(cpu_s), max(bytes_read), max(reads), bool_and(ok),
               max_by(error, seq)
        FROM m GROUP BY ALL""").fetchall()
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for variant, query, kind, n, med, lo, hi, mean, sd, cpu, nbytes, reads, ok, error in agg:
        cell = cells.setdefault((variant, query), {"ok": True})
        if kind in ("cold", "warm"):
            cell[kind] = {"n": n, "median_s": med, "min_s": lo, "max_s": hi, "stdev_s": sd,
                          "cv": sd / mean if mean else 0.0}
            if kind == "warm":
                cell["cpu_s"] = cpu
        elif kind == "counted":
            cell.update(bytes_read=nbytes, reads=reads)
        else:
            cell["error"] = error
        if ok is False:
            cell["ok"] = False

    csv = manifest.get("csv-none", {}).get("bytes") or meta["landing_bytes"]
    variants, missing = [], []
    for v in MATRIX:
        m = manifest.get(v.id)
        if m is None or m.get("source") != fp:
            continue
        queries = {}
        for q in ds["queries"]:
            cell = cells.get((v.id, q["id"]), {"ok": False})
            if "warm" in cell:
                cell.pop("error", None)  # an earlier failure that a later run of the cell superseded
                cell["read_fraction"] = cell["bytes_read"] / m["bytes"]
            if "warm" not in cell or not cell["ok"]:
                missing.append(f"{v.id}/{q['id']}")
            queries[q["id"]] = cell
        variants.append({k: m[k] for k in ("id", "format", "codec", "level", "chunk", "layout", "reader", "bytes",
                                           "rows", "write_s", "write_cpu_s", "chunks")}
                        | {"ratio_vs_csv": csv / m["bytes"], "queries": queries})
    if not variants:
        raise DataError("No variants match the current source. Run `bakeoff materialize` and `bakeoff bench`.")

    types = dict(meta["schema"])
    real_columns = [{"variant": vid, "column": c["column"], "type": types.get(c["column"], "?"),
                     "compressed": c["compressed"], "uncompressed": c["uncompressed"],
                     "ratio": c["uncompressed"] / c["compressed"], "encodings": c["encodings"]}
                    for vid, m in manifest.items() if m.get("source") == fp for c in m.get("columns", [])]
    env = read_json(res / "env.json", {})
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dataset": {"name": meta["dataset"], "sort_key": meta["sort_key"], "rows": meta["clean"], "csv_bytes": csv,
                    "landed": meta["landed"], "quarantined": meta["quarantined"], "warnings": meta["warnings"],
                    "source": s.source, "months": list(s.months), "columns": len(meta["schema"])},
        "env": env,
        "queries": [{k: q.get(k) for k in ("id", "class", "sql", "description", "control")} for q in ds["queries"]],
        "variants": variants,
        "pushdown": pushdown(variants, ds["queries"]),
        "columns": {"lab": read_json(res / "columns.json", {"columns": [], "cells": [], "rows": 0}),
                    "real": real_columns},
        "stages": read_json(s.data_dir / "stages.json", {}),
        "partial": bool(missing), "missing": missing,
    }


def pushdown(variants: list[dict[str, Any]], queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per variant: what fraction of the file each query pulled from storage, and for filter
    queries how much less than their no-filter control. That difference is predicate pushdown."""
    out = []
    for v in variants:
        row: dict[str, Any] = {"variant": v["id"], "format": v["format"], "reader": v["reader"],
                               "read_fraction": {}, "pruned_vs_control": {}}
        for q in queries:
            cell = v["queries"].get(q["id"], {})
            if "bytes_read" not in cell:
                continue
            row["read_fraction"][q["id"]] = cell["bytes_read"] / v["bytes"]
            control = v["queries"].get(q.get("control", ""), {}).get("bytes_read")
            if control:
                row["pruned_vs_control"][q["id"]] = 1 - cell["bytes_read"] / control
        # Judged against the most any query read from this file, so a footer-heavy small file
        # is not mistaken for a reader that ignores projections.
        projection = row["read_fraction"].get("projection")
        clustered = row["pruned_vs_control"].get("filter_clustered")
        most = max([1.0, *row["read_fraction"].values()])
        row["projection_pushdown"] = None if projection is None else projection < 0.9 * most
        row["predicate_pushdown"] = None if clustered is None else clustered > 0.1
        out.append(row)
    return out


def report(s: Settings, ds: dict[str, Any], info: dict[str, Any], publish: bool = False) -> None:
    results = build(s, ds)
    write_json(s.data_dir / "results" / "results.json", results)
    if publish:
        s.published_dir.mkdir(parents=True, exist_ok=True)
        for name in ("results.json", "measurements.parquet"):
            shutil.copyfile(s.data_dir / "results" / name, s.published_dir / name)
    info.update(rows=sum(len(v["queries"]) for v in results["variants"]), partial=results["partial"])


# ---------------------------------------------------------------- cost model

class Workload(BaseModel):
    """A described workload. All sizes are in terms of the data as uncompressed CSV."""

    dataset_gb: float = Field(100, gt=0, le=10_000_000,
                              description="Dataset size as uncompressed CSV, GB (10^9 bytes).")
    queries_per_month: int = Field(10_000, ge=0, le=1_000_000_000)
    mix: dict[str, float] = Field(default_factory=dict, description="Relative weight per query class. "
                                  "Empty = every class in the workload equally.")
    rewrites_per_month: float = Field(1, ge=0, le=1000, description="How many times a month the whole dataset "
                                      "is (re)encoded. 0.1 = 10% new data a month.")
    provider: Literal["aws", "gcp", "azure"] = "aws"
    engine: Literal["serverless", "self_hosted"] = Field(
        "serverless", description="serverless = pay per byte scanned; self_hosted = pay for CPU time.")
    objective: Literal["cost", "speed", "balanced"] = "balanced"


class WorkloadError(ValueError):
    pass


def pricing() -> dict[str, Any]:
    return load_toml("pricing.toml")


def monthly_cost(variant: dict[str, Any], classes: dict[str, list[str]], w: Workload, mix: dict[str, float],
                 price: dict[str, Any], csv_bytes: int) -> dict[str, float]:
    """Monthly USD for `variant` under workload `w`, extrapolated linearly from the measured dataset.

      storage   stored bytes x $/GB-month
      scan      serverless: bytes read per query (at least the provider minimum) x $/TB
      compute   self-hosted: measured warm CPU-seconds per query x $/vCPU-hour
      requests  read calls per query x $/1000 GET
      write     measured encode CPU-seconds x rewrites x $/vCPU-hour
    """
    scale = w.dataset_gb * 1e9 / csv_bytes
    cost = {"storage": variant["bytes"] * scale / 1e9 * price["storage_gb_month"], "scan": 0.0, "compute": 0.0,
            "requests": 0.0, "write": w.rewrites_per_month * variant["write_cpu_s"] * scale / 3600 * price["vcpu_hour"]}
    latency = 0.0
    for cls, weight in mix.items():
        cells = [variant["queries"][q] for q in classes[cls]]
        n = w.queries_per_month * weight
        nbytes = sum(c["bytes_read"] for c in cells) / len(cells) * scale
        reads = max(1.0, sum(c["reads"] for c in cells) / len(cells) * scale)
        cost["requests"] += n * reads / 1000 * price["get_per_1k"]
        if w.engine == "serverless":
            cost["scan"] += n * max(nbytes, price["min_scan_mb"] * 1e6) / 1e12 * price["scan_per_tb"]
        else:
            cost["compute"] += n * sum(c["cpu_s"] for c in cells) / len(cells) * scale / 3600 * price["vcpu_hour"]
        latency += weight * sum(c["warm"]["median_s"] for c in cells) / len(cells)
    return {**cost, "total": sum(cost.values()), "latency_s": latency}


def recommend(results: dict[str, Any], w: Workload) -> dict[str, Any]:
    """Rank every candidate variant for workload `w` and explain the pick in numbers."""
    classes: dict[str, list[str]] = {}
    for q in results["queries"]:
        classes.setdefault(q["class"], []).append(q["id"])
    unknown = sorted(set(w.mix) - set(classes))
    if unknown:
        raise WorkloadError(f"Unknown query class {unknown}. This workload has: {sorted(classes)}")
    weights = {c: x for c, x in (w.mix or dict.fromkeys(classes, 1.0)).items() if x > 0}
    if any(x < 0 for x in w.mix.values()) or not weights:
        raise WorkloadError("mix weights must be non-negative and at least one must be positive")
    total = sum(weights.values())
    mix = {c: x / total for c, x in weights.items()}
    price = pricing()["providers"][w.provider]
    needed = [q for c in mix for q in classes[c]]

    ranked, excluded = [], []
    for v in results["variants"]:
        cells = [v["queries"].get(q, {}) for q in needed]
        if v["layout"] != "sorted":
            excluded.append({"variant": v["id"], "reason": "control variant, not a storage layout to adopt"})
        elif not all("warm" in c and c.get("ok") and "bytes_read" in c for c in cells):
            excluded.append({"variant": v["id"], "reason": "missing or failed measurements for this mix"})
        else:
            ranked.append({"variant": v["id"], "format": v["format"], "codec": v["codec"],
                           **monthly_cost(v, classes, w, mix, price, results["dataset"]["csv_bytes"])})
    if not ranked:
        raise WorkloadError("No variant has complete measurements for this mix")
    cheapest = min(r["total"] for r in ranked)
    fastest = min(r["latency_s"] for r in ranked)
    for r in ranked:
        r["score"] = {"cost": r["total"], "speed": r["latency_s"],
                      "balanced": r["total"] / cheapest + r["latency_s"] / fastest}[w.objective]
    ranked.sort(key=lambda r: (r["score"], r["variant"]))
    pick = ranked[0]
    base = next((r for r in ranked if r["variant"] == "csv-none"), None)
    why = [f"{pick['variant']} costs ${pick['total']:,.2f}/month for {w.dataset_gb:g} GB and "
           f"{w.queries_per_month:,} queries on {price['label']}."]
    parts = {k: pick[k] for k in ("storage", "scan", "compute", "requests", "write") if pick[k] > 0}
    top = max(parts, key=parts.__getitem__)
    why.append(f"Its largest cost is {top} at ${parts[top]:,.2f} ({parts[top] / pick['total']:.0%} of the bill).")
    if base and base is not pick:
        why.append(f"Uncompressed CSV would cost ${base['total']:,.2f}/month, "
                   f"{base['total'] / pick['total']:.1f}x more, and answer the mix "
                   f"{base['latency_s'] / pick['latency_s']:.1f}x slower.")
    if len(ranked) > 1:
        nxt = ranked[1]
        why.append(f"Runner-up {nxt['variant']}: ${nxt['total']:,.2f}/month, "
                   f"{nxt['latency_s'] / pick['latency_s']:.2f}x its query time.")
    return {"workload": w.model_dump() | {"mix": mix}, "price": price, "pick": pick["variant"], "why": why,
            "ranked": ranked, "excluded": excluded,
            "assumptions": ["Linear extrapolation from the measured dataset size.",
                            "Latency is the measured warm median on the benchmark machine, weighted by the mix; "
                            "it is not scaled to the described dataset.",
                            "Same-region access: no egress. List prices, first tier, no free tier."]}


# ---------------------------------------------------------------- generated docs

def write_docs(s: Settings, out: Path) -> None:
    """Data dictionary from the dataset contract; data profile from the last profile/ingest run."""
    ds = load_toml("taxi.toml")
    lines = ["# Data dictionary", "", "Generated by `bakeoff docs` from `bakeoff/taxi.toml`. Do not edit by hand.", "",
             f"## `{ds['dataset']['name']}`", "", ds["dataset"]["description"],
             f"Every variant holds these columns, sorted by `{ds['dataset']['sort_key']}`.", "",
             "| Column | Source header | Type | Nullable | Unit | Meaning |", "|---|---|---|---|---|---|"]
    lines += [f"| `{n}` | `{src}` | {t} | {'yes' if null else 'no'} | {unit} | {desc} |"
              for n, src, t, null, unit, desc in ds["dataset"]["columns"]]
    lines += ["", "## Validation rules", "", "A quarantined row goes to `quarantine/rejects.parquet` with its reason. "
              "A counted row is kept and tallied in `clean/ingest.json`.", "",
              "| Rule | Action | Condition | Why |", "|---|---|---|---|",
              "| `unparseable` | quarantine | the CSV line does not parse to the column types | "
              "Wrong field count, text in a numeric column, a non-ISO timestamp. |",
              "| `null_in_required` | quarantine | NULL in a non-nullable column | The contract above. |",
              "| `duplicate` | quarantine extra copies | row identical to another in all columns | "
              "An upstream replay. |"]
    lines += [f"| `{r['id']}` | {'quarantine' if r['quarantine'] else 'count'} | `{r['when']}` | {r['why']} |"
              for r in ds["rules"]]
    lines += ["", "## `quarantine/rejects.parquet`", "", "| Column | Type | Nullable | Meaning |", "|---|---|---|---|",
              "| `reason` | VARCHAR | no | Rule id, `duplicate`, or `unparseable`. |",
              "| `copies` | INTEGER | no | Rows this record accounts for (extra copies, for duplicates). |",
              "| `line` | BIGINT | yes | Line number in the landing CSV; set for unparseable lines only. |",
              "| `raw` | VARCHAR | no | The row as JSON, or the original CSV line and the parser's error. |",
              "", "## `results/measurements.parquet`", "", "One row per benchmark execution.", "",
              "| Column | Type | Nullable | Unit | Meaning |", "|---|---|---|---|---|",
              "| `seq` | BIGINT | no | | Stable order of executions; the pagination cursor. |",
              "| `variant` | VARCHAR | no | | Variant id, e.g. `parquet-zstd3-rg100k`. |",
              "| `query` | VARCHAR | no | | Query id from the workload. |",
              "| `kind` | VARCHAR | no | | `cold`, `warm`, `counted` or `error`. |",
              "| `ts` | VARCHAR | no | UTC ISO-8601 | When the execution finished. |",
              "| `seconds` | DOUBLE | yes | s | Wall time. NULL for `counted` and `error`. |",
              "| `cpu_s` | DOUBLE | yes | s | Process CPU time, all threads. |",
              "| `bytes_read` | BIGINT | yes | bytes | Bytes the reader pulled from storage; `counted` only. |",
              "| `reads` | BIGINT | yes | calls | Read calls made to storage; `counted` only. |",
              "| `ok` | BOOLEAN | yes | | Result matched the answer from `clean/source.parquet`. |",
              "| `error` | VARCHAR | yes | | Exception text; `error` only. |", ""]
    out.mkdir(parents=True, exist_ok=True)
    (out / "data-dictionary.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")

    prof = read_json(s.data_dir / "landing" / "profile.json")
    meta = read_json(s.data_dir / "clean" / "ingest.json")
    if prof and meta:
        p = ["# Data profile", "", f"Generated by `bakeoff docs` from the landing CSV ({s.source}, "
             f"{', '.join(s.months)}; {prof['bytes'] / 1e6:,.0f} MB, {meta['landed']:,} lines), before any validation.",
             "", "| Column | Sniffed type | Min | Max | ~Distinct | Null % |", "|---|---|---|---|---|---|"]
        p += [f"| `{c['column']}` | {c['sniffed_type']} | {c['min']} | {c['max']} | {c['approx_distinct']:,} | "
              f"{c['null_pct']:.2f} |" for c in prof["columns"]]
        p += ["", "## What ingest did with it", "", f"- landed: **{meta['landed']:,}** lines",
              f"- clean: **{meta['clean']:,}** rows", "- quarantined:"]
        p += [f"  - `{k}`: {v:,}" for k, v in meta["quarantined"].items()] or ["  - nothing"]
        p += ["- kept but counted:"] + [f"  - `{k}`: {v:,}" for k, v in meta["warnings"].items()]
        (out / "data-profile.md").write_text("\n".join(p) + "\n", encoding="utf-8", newline="\n")
