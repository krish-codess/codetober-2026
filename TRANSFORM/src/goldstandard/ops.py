"""Operational evidence generators: query plans and throughput measurements, committed under docs/.

goldstandard explain   -> docs/explain/*.txt   EXPLAIN (ANALYZE, BUFFERS) of every hot query
goldstandard bench     -> docs/perf/bench.json stage throughput + API latency, measured
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from goldstandard.config import Settings

# name -> (why this index exists, SQL, params-producing SQL or literal params)
QUERIES: dict[str, tuple[str, str]] = {
    "index_series": (
        "GET /v1/index: one series, a date range, latest vintage per day. Served by the index_value PK "
        "(series_id, day, vintage): DISTINCT ON walks it in order, no sort.",
        """SELECT day, value, coverage, status, vintage, revised FROM index_value_current
           WHERE series_id = %(series_id)s AND day BETWEEN %(lo)s AND %(hi)s ORDER BY day""",
    ),
    "index_as_of": (
        "GET /v1/index?as_of=: reproduce what was published at an instant. Same PK, filter on computed_at.",
        """SELECT DISTINCT ON (day) day, value, vintage FROM index_value
           WHERE series_id = %(series_id)s AND day BETWEEN %(lo)s AND %(hi)s AND computed_at <= now()
           ORDER BY day, vintage DESC""",
    ),
    "purchasing_power_prices": (
        "GET /v1/purchasing-power: a handful of items on one server over a range. Served by the "
        "item_price_daily PK (server_id, item_id, day, vintage).",
        """SELECT day, item_id, price FROM item_price_current
           WHERE server_id = %(server_id)s AND item_id = ANY(%(items)s) AND day BETWEEN %(lo)s AND %(hi)s
             AND price IS NOT NULL""",
    ),
    "patches_keyset_page": (
        "GET /v1/patches: keyset page ordered by (released_at, patch_id). Served by patch_event_timeline "
        "(world_id, released_at, patch_id): an index range scan that stops after LIMIT rows.",
        """SELECT patch_id, released_at, title FROM patch_event
           WHERE world_id = %(world)s AND (released_at, patch_id) > (%(after)s, '') ORDER BY released_at, patch_id
           LIMIT 101""",
    ),
    "manipulation_feed": (
        "GET /v1/manipulation: newest-first keyset page for one server. Served by manipulation_event_feed "
        "(server_id, day DESC, item_id DESC, kind DESC), matching the ORDER BY exactly (migration 0003).",
        """SELECT server_id, item_id, day, kind, severity FROM manipulation_event
           WHERE server_id = %(server_id)s AND (day, item_id, kind) < ('9999-12-31'::date, 9223372036854775807, '~')
           ORDER BY day DESC, item_id DESC, kind DESC LIMIT 51""",
    ),
    "inflation_matrix": (
        "GET /v1/inflation/matrix: latest value per series - one backward probe of the inflation_rate PK "
        "(series_id, window_days, day) per series via LATERAL ... LIMIT 1 (a DISTINCT ON version seq-scanned "
        "and sorted every row: 18 ms vs well under 1 ms).",
        """SELECT s.series_id, r.day, r.annualized FROM index_series s CROSS JOIN LATERAL (
             SELECT day, annualized FROM inflation_rate WHERE series_id = s.series_id AND window_days = 30
             ORDER BY day DESC LIMIT 1) r WHERE s.world_id = %(world)s ORDER BY s.series_id""",
    ),
    "publish_vintage_lookup": (
        "Pipeline publish: for each staged value, find the current vintage via LATERAL ... ORDER BY "
        "vintage DESC LIMIT 1 - one PK probe per row instead of materialising item_price_current.",
        """EXPLAIN_IN_TXN
           CREATE TEMP TABLE stage_price ON COMMIT DROP AS
             SELECT server_id, item_id, day, price FROM item_price_current WHERE day = %(hi)s;
           SELECT count(*) FROM stage_price s LEFT JOIN LATERAL (
             SELECT p.vintage FROM item_price_daily p
             WHERE p.server_id = s.server_id AND p.item_id = s.item_id AND p.day = s.day
             ORDER BY p.vintage DESC LIMIT 1) c ON true""",
    ),
}


def explain(dsn: str, out_dir: Path, world: str = "synthetic") -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with psycopg.connect(dsn) as conn:
        sid = conn.execute(
            """SELECT series_id FROM index_series WHERE world_id = %s AND server_id IS NULL
                              AND division_id IS NULL""",
            (world,),
        ).fetchone()
        server = conn.execute(
            "SELECT server_id FROM server WHERE world_id = %s ORDER BY server_id LIMIT 1", (world,)
        ).fetchone()
        hi = conn.execute("SELECT max(day) FROM item_price_daily").fetchone()
        if not (sid and server and hi and hi[0]):
            raise SystemExit("no data to explain: run the pipeline first")
        sizes: dict[str, int] = {}
        for t in ("item_price_daily", "index_value", "patch_event", "manipulation_event", "inflation_rate"):
            row = conn.execute(f"SELECT count(*) FROM {t}").fetchone()  # noqa: S608 - fixed table names
            sizes[t] = int(row[0]) if row else 0
        params = {
            "series_id": sid[0],
            "server_id": server[0],
            "world": world,
            "lo": hi[0] - timedelta(days=365),
            "hi": hi[0],
            "items": [34, 35, 587],
            "after": datetime(1970, 1, 1, tzinfo=UTC),
        }
        for name, (why, sql) in QUERIES.items():
            if sql.strip().startswith("EXPLAIN_IN_TXN"):
                setup, query = sql.strip().removeprefix("EXPLAIN_IN_TXN").strip().split(";", 1)
                with conn.transaction(force_rollback=True):
                    conn.execute(setup, params)
                    conn.execute("ANALYZE stage_price")
                    plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + query.strip(), params).fetchall()
            else:
                plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + sql, params).fetchall()
            text = "\n".join(r[0] for r in plan)
            captured = f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC, PostgreSQL {conn.info.server_version}"
            body = (
                f"-- {name}\n-- {why}\n-- table sizes: {json.dumps(sizes)}\n-- captured {captured}\n\n"
                f"{' '.join(sql.split())}\n\n{text}\n"
            )
            (out_dir / f"{name}.txt").write_text(body, encoding="utf-8", newline="\n")
            written.append(name)
    return written


def bench(cfg: Settings, days: int, servers: int, api_url: str | None) -> dict[str, Any]:
    """Measure every stage on a fresh synthetic world (no database writes), plus API latency if given."""
    import polars as pl

    from goldstandard import estimators, index, parse
    from goldstandard.fills import infer_fills
    from goldstandard.raw import RawStore
    from goldstandard.sources import synthetic

    out: dict[str, Any] = {
        "machine": {"platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()},
        "params": {"days": days, "servers": servers},
    }
    with tempfile.TemporaryDirectory() as tmp:
        store = RawStore(Path(tmp) / "raw")
        sc = synthetic.SynthConfig(start=date(2025, 9, 1), servers=servers, seed=1)
        until = datetime(2025, 9, 1, tzinfo=UTC) + timedelta(days=days)
        t0 = time.perf_counter()
        gen = synthetic.generate(sc, store, cfg.reference_dir, until)
        t_gen = time.perf_counter() - t0
        out["generate"] = {
            "seconds": round(t_gen, 2),
            "orders": gen["orders"],
            "bundles": gen["bundles_written"],
            "orders_per_s": round(gen["orders"] / t_gen),
        }
        universe = json.loads((cfg.reference_dir / "eve_universe.json").read_text(encoding="utf-8"))
        items = {i["type_id"] for i in universe["items"]}
        t_parse = t_est = t_fill = 0.0
        rows = 0
        dailies = []
        expected = pl.DataFrame({"server_id": sc.server_ids}).join(
            pl.DataFrame({"item_id": sorted(items)}, schema={"item_id": pl.Int64}), how="cross"
        )
        for d in store.days("synthetic", "orders"):
            recs = [store.read(r) for r in store.iter_refs("synthetic", "orders", d)]
            t0 = time.perf_counter()
            p = parse.parse_order_bundles("synthetic", recs, items, set(sc.server_ids))
            t_parse += time.perf_counter() - t0
            rows += p.valid.height + p.quarantine.height
            t0 = time.perf_counter()
            daily = estimators.daily_from_snapshots(estimators.snapshot_prices(p.valid), expected, d)
            t_est += time.perf_counter() - t0
            t0 = time.perf_counter()
            fills = infer_fills(p.valid)
            t_fill += time.perf_counter() - t0
            vol = fills.group_by("server_id", "item_id").agg(volume=pl.col("fill_qty").sum().cast(pl.Float64))
            dailies.append(daily.join(vol, on=["server_id", "item_id"], how="left").rename({"price": "raw_price"}))
        out["parse_validate"] = {"seconds": round(t_parse, 2), "rows": rows, "rows_per_s": round(rows / t_parse)}
        out["estimate"] = {"seconds": round(t_est, 2), "per_day_ms": round(1000 * t_est / days, 1)}
        out["fill_inference"] = {"seconds": round(t_fill, 2), "per_day_ms": round(1000 * t_fill / days, 1)}
        t0 = time.perf_counter()
        acc = estimators.robust_series(
            pl.concat(dailies, how="vertical_relaxed").select(
                "server_id", "item_id", "day", "raw_price", "status", "volume"
            )
        )
        out["robust_acceptance"] = {"seconds": round(time.perf_counter() - t0, 2)}
        periods = index.quarter_periods("synthetic", date(2025, 9, 1), until.date())
        t0 = time.perf_counter()
        n_vals = 0
        divisions = {i["type_id"]: i["division"] for i in universe["items"]}
        for per in periods:
            basket = index.build_basket(per, acc.select("server_id", "item_id", "day", "price", "volume"), divisions)
            prices = acc.filter(pl.col("day").is_between(per.valid_from, per.valid_to) & pl.col("price").is_not_null())
            links = {(s, d): 100.0 for s in [*sc.server_ids, "all"] for d in [*set(divisions.values()), "all"]}
            n_vals += index.index_values(basket, prices, links, sc.server_ids, sorted(set(divisions.values()))).height
        out["index"] = {"seconds": round(time.perf_counter() - t0, 2), "values": n_vals}
    if api_url:
        import httpx

        lat: dict[str, Any] = {}
        with httpx.Client(base_url=api_url, timeout=10) as http:
            for path in (
                "/v1/worlds",
                "/v1/index?world=eve",
                "/v1/index?world=synthetic&division=fuel",
                "/v1/patches?world=eve&limit=100",
                "/v1/inflation/matrix?world=eve&window=30",
                "/v1/purchasing-power?world=synthetic&server=syn-aurora&activity=syn-ratting&items=34,587",
                "/v1/manipulation?world=synthetic&server=syn-drift&limit=50",
                "/health/ready",
            ):
                samples = []
                for _ in range(40):
                    t0 = time.perf_counter()
                    http.get(path, headers={"Cache-Control": "no-cache"}).raise_for_status()
                    samples.append((time.perf_counter() - t0) * 1000)
                samples.sort()
                lat[path] = {
                    "p50_ms": round(statistics.median(samples), 1),
                    "p95_ms": round(samples[int(0.95 * len(samples)) - 1], 1),
                }
        out["api_latency"] = lat
    return out


def render_api_markdown(spec: dict[str, Any]) -> str:
    """docs/API.md from the OpenAPI spec (which FastAPI generates from the code)."""
    schemas = spec.get("components", {}).get("schemas", {})

    def type_of(s: dict[str, Any]) -> str:
        if "$ref" in s:
            return f"`{s['$ref'].rsplit('/', 1)[-1]}`"
        if "anyOf" in s:
            return " or ".join(type_of(x) for x in s["anyOf"])
        if s.get("type") == "array":
            return f"array of {type_of(s.get('items', {}))}"
        extra = f" `{s['pattern']}`" if "pattern" in s else ""
        if "enum" in s:
            extra = " (" + ", ".join(f"`{e}`" for e in s["enum"]) + ")"
        return f"{s.get('type', 'object')}{s.get('format', '') and ' (' + s['format'] + ')'}{extra}"

    out = [
        f"# API reference: {spec['info']['title']} {spec['info']['version']}",
        "",
        "Generated from `docs/api/openapi.json`, which is generated from the FastAPI code by "
        "`goldstandard openapi`. CI fails if either file is out of date. "
        "Interactive version: `/docs` on a running API.",
        "",
        spec["info"].get("description", ""),
        "",
        '**Errors** always have the shape `{"error": {"code", "message", "details", "request_id"}}`. '
        "Every response carries `X-Request-ID`. "
        "GET responses under `/v1` carry an `ETag` and honour `If-None-Match` (304).",
        "",
    ]
    for path, methods in sorted(spec["paths"].items()):
        for method, op in methods.items():
            out += [f"## `{method.upper()} {path}`", "", op.get("summary", ""), ""]
            params = op.get("parameters", [])
            if params:
                out += ["| Parameter | In | Required | Type | Description |", "|---|---|---|---|---|"]
                for p in params:
                    desc = p.get("description", p.get("schema", {}).get("description", ""))
                    req = "yes" if p.get("required") else "no"
                    out.append(f"| `{p['name']}` | {p['in']} | {req} | {type_of(p.get('schema', {}))} | {desc} |")
                out.append("")
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            if body:
                out += [f"Request body: {type_of(body)}", ""]
            out += ["| Status | Meaning |", "|---|---|"]
            for code, r in sorted(op.get("responses", {}).items()):
                schema = r.get("content", {}).get("application/json", {}).get("schema")
                out.append(f"| {code} | {r.get('description', '')}{' - ' + type_of(schema) if schema else ''} |")
            out.append("")
    out += ["## Schemas", ""]
    for name, s in sorted(schemas.items()):
        out += [f"### `{name}`", ""]
        if s.get("description"):
            out += [s["description"], ""]
        props = s.get("properties", {})
        if props:
            required = set(s.get("required", []))
            out += ["| Field | Type | Required | Description |", "|---|---|---|---|"]
            for f, fs in props.items():
                flag = "yes" if f in required else "no"
                out.append(f"| `{f}` | {type_of(fs)} | {flag} | {fs.get('description', '')} |")
            out.append("")
    return "\n".join(out) + "\n"


def _num(x: Any) -> float:
    """Polars scalars come back as a broad union; the accuracy report only needs floats."""
    return float("nan") if x is None else float(x)


def accuracy(cfg: Settings) -> dict[str, Any]:
    """Score the published synthetic world against the generator's ground truth (lake/synthetic_truth)."""
    import polars as pl

    from goldstandard import index
    from goldstandard.db import connect
    from goldstandard.lake import Lake

    truth_dir = cfg.lake_dir / "synthetic_truth"
    fair = pl.read_parquet(truth_dir / "fair.parquet")
    out: dict[str, Any] = {}
    with connect(cfg) as conn:

        def frame(sql: str, schema: list[str], params: tuple[Any, ...] = ()) -> pl.DataFrame:
            return pl.DataFrame(conn.execute(sql, params).fetchall(), schema=schema, orient="row")

        servers = [r[0] for r in conn.execute("SELECT server_id FROM server WHERE world_id = 'synthetic'").fetchall()]
        divisions = [r[0] for r in conn.execute("SELECT division_id FROM division").fetchall()]
        periods = frame(
            """SELECT period_id, valid_from, valid_to, link_from, link_to FROM basket_period
                           WHERE world_id = 'synthetic' ORDER BY valid_from""",
            ["period_id", "valid_from", "valid_to", "link_from", "link_to"],
        )
        published = frame(
            """SELECT v.day, v.value FROM index_value_current v JOIN index_series s USING (series_id)
                             WHERE s.world_id = 'synthetic' AND s.server_id IS NULL AND s.division_id IS NULL
                               AND v.value IS NOT NULL""",
            ["day", "value"],
        )
        # 1. the same frozen baskets, evaluated on TRUE fair prices, chain-linked the same way
        truth_vals = []
        link = 100.0
        prev = None
        fair_p = fair.rename({"fair": "price"})
        for per in periods.iter_rows(named=True):
            cells = frame(
                """SELECT b.server_id, b.item_id, i.division_id, b.weight FROM basket_item b
                             JOIN item i USING (item_id) WHERE b.world_id = 'synthetic' AND b.period_id = %s""",
                ["server_id", "item_id", "division_id", "weight"],
                (per["period_id"],),
            )
            base = (
                fair_p.filter(pl.col("day").is_between(per["link_from"], per["link_to"]))
                .group_by("server_id", "item_id")
                .agg(base_price=pl.col("price").median())
            )
            basket = cells.join(base, on=["server_id", "item_id"])
            if prev is not None:
                j = index.link_factors(prev, base.rename({"base_price": "price"}), servers, divisions)
                link *= j[(index.ALL, index.ALL)]
            vals = index.index_values(
                basket,
                fair_p.filter(pl.col("day").is_between(per["valid_from"], per["valid_to"])),
                {(index.ALL, index.ALL): link},
                servers,
                divisions,
            )
            truth_vals.append(
                vals.filter((pl.col("scope") == index.ALL) & (pl.col("division") == index.ALL)).select(
                    "day", pl.col("value").alias("truth")
                )
            )
            prev = basket
        cmp = published.join(pl.concat(truth_vals), on="day").with_columns(
            err=(pl.col("value") / pl.col("truth")).log().abs()
        )
        out["index_vs_truth"] = {
            "days": cmp.height,
            "mean_abs_log_error": round(_num(cmp["err"].mean() or 0), 4),
            "p95_abs_log_error": round(_num(cmp["err"].quantile(0.95) or 0), 4),
            "max_abs_log_error": round(_num(cmp["err"].max() or 0), 4),
            "final_level_published": round(_num(cmp["value"][-1]), 2),
            "final_level_truth": round(_num(cmp["truth"][-1]), 2),
        }
        # 2. manipulation detection recall (single troll / bait listings -> extreme_listing on that day)
        manip = pl.read_parquet(truth_dir / "manipulations.parquet")
        events = frame(
            """SELECT m.server_id, m.item_id, m.day, m.kind FROM manipulation_event m
                          JOIN server s USING (server_id) WHERE s.world_id = 'synthetic'""",
            ["server_id", "item_id", "day", "kind"],
        )
        rec: dict[str, Any] = {}
        caught_by = {
            "absurd_listing": ["extreme_listing"],
            "bait_listing": ["extreme_listing"],
            "corner": ["thin_market_spike", "rejected_price"],
        }  # a corner is caught if flagged OR rejected
        for kind, flagged in caught_by.items():
            t = manip.filter(pl.col("kind") == kind)
            hit = t.join(events.filter(pl.col("kind").is_in(flagged)), on=["server_id", "item_id", "day"], how="semi")
            rec[kind] = {
                "injected": t.height,
                "caught_same_day": hit.height,
                "caught_by": flagged,
                "recall": round(hit.height / t.height, 3) if t.height else None,
            }
        out["manipulation_recall"] = rec
        # does any injected manipulation reach a published price? count published prices > 4x the true fair
        prices = frame(
            """SELECT server_id, item_id, day, price FROM item_price_current
                          WHERE server_id LIKE 'syn-%%' AND price IS NOT NULL""",
            ["server_id", "item_id", "day", "price"],
        )
        pf = prices.join(fair, on=["server_id", "item_id", "day"]).with_columns(
            r=(pl.col("price") / pl.col("fair")).log().abs()
        )
        distorted = pf.filter(pl.col("r") > 0.5596)  # ln 1.75: materially wrong, whatever the cause
        flagged_rows = distorted.join(
            events.filter(pl.col("kind") == "thin_market_spike"), on=["server_id", "item_id", "day"], how="semi"
        )
        out["distorted_published_prices"] = {
            "count": distorted.height,
            "share_of_published": round(distorted.height / max(pf.height, 1), 5),
            "flagged_as_thin_market_spike": flagged_rows.height,
            "flagged_share": round(flagged_rows.height / distorted.height, 3) if distorted.height else None,
        }
        out["published_prices_vs_fair"] = {
            "prices": pf.height,
            "median_abs_log_error": round(_num(pf["r"].median() or 0), 4),
            "share_beyond_2x": round(_num((pf["r"] > 0.693).mean() or 0), 5),
            "share_beyond_4x": round(_num((pf["r"] > 1.386).mean() or 0), 5),
        }
    # 3. fill inference vs true volume
    lake = Lake(cfg.lake_dir)
    fills = lake.scan("fills", "synthetic", columns=["server_id", "item_id", "snapshot_ts", "fill_qty"])
    inferred = (
        fills.with_columns(day=pl.col("snapshot_ts").dt.date())
        .group_by("server_id", "item_id", "day")
        .agg(pl.col("fill_qty").sum())
    )
    true_fills = pl.read_parquet(truth_dir / "fills.parquet")
    jf = true_fills.join(inferred, on=["server_id", "item_id", "day"], how="inner")
    out["fill_inference"] = {
        "cells": jf.height,
        "inferred_over_true_volume": round(_num(jf["fill_qty"].sum()) / _num(jf["qty"].sum()), 3),
        "log_correlation": round(_num(jf.select(pl.corr(pl.col("qty").log1p(), pl.col("fill_qty").log1p()))[0, 0]), 3),
    }
    return out
