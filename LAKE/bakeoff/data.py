"""Data foundation: fetch -> land -> profile -> ingest. Every stage is idempotent.

    raw/        immutable TLC downloads + MANIFEST.json (sha256)
    landing/    trips.csv, the one big CSV, as it arrived, defects intact
    clean/      source.parquet (validated, deduplicated, sorted) + ingest.json
    quarantine/ rejects.parquet, every rejected row with its reason
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from .config import Settings, load_toml, log, read_json, write_json


class DataError(RuntimeError):
    """The data, or a source of it, is not what the pipeline can accept. Message says what to do."""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


def connect(s: Settings) -> duckdb.DuckDBPyConnection:
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(config={"threads": s.threads, "temp_directory": str(s.data_dir / "tmp")})


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------- fetch

def download(url: str, dest: Path, attempts: int = 5, backoff: float = 2.0, timeout: float = 60) -> None:
    """Download to dest atomically. Retries transient failures with capped exponential backoff."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r, part.open("wb") as f:  # noqa: S310
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            os.replace(part, dest)
            return
        except urllib.error.HTTPError as e:
            if e.code not in (408, 429) and e.code < 500:  # a 404 will not heal by asking again
                raise DataError(f"{url} returned HTTP {e.code}. Check LAKE_MONTHS and LAKE_TLC_BASE_URL, "
                                "or set LAKE_SOURCE=synthetic to run offline.") from e
            err: Exception = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
        if attempt < attempts:
            delay = min(30.0, backoff ** attempt) * random.uniform(0.5, 1.0)  # noqa: S311
            log("fetch.retry", logging.WARNING, url=url, attempt=attempt, error=str(err), sleep_s=round(delay, 2))
            time.sleep(delay)
    part.unlink(missing_ok=True)
    raise DataError(f"{url} failed {attempts} times ({err}). The source is unreachable; "
                    "set LAKE_SOURCE=synthetic to run the same pipeline on generated data.")


def fetch(s: Settings, info: dict[str, Any]) -> None:
    """Download the configured TLC months. Raw files are write-once: a changed hash is an error."""
    raw = s.data_dir / "raw"
    manifest = read_json(raw / "MANIFEST.json", {})
    for month in s.months:
        name = f"yellow_tripdata_{month}.parquet"
        dest, url = raw / name, f"{s.tlc_base_url}/{name}"
        if not dest.exists():
            download(url, dest)
            dest.chmod(stat.S_IREAD)
        digest = sha256(dest)
        known = manifest.get(name)
        if known and known["sha256"] != digest:
            raise DataError(f"{dest} no longer matches the hash recorded when it was fetched. Raw inputs are "
                            "immutable; delete the file and its MANIFEST.json entry to re-download.")
        manifest.setdefault(name, {"sha256": digest, "bytes": dest.stat().st_size, "url": url,
                                   "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    write_json(raw / "MANIFEST.json", manifest)
    info.update(files=len(s.months), bytes=sum(manifest[f"yellow_tripdata_{m}.parquet"]["bytes"] for m in s.months))


# ---------------------------------------------------------------- land

def synth_taxi(path: Path, rows: int, seed: int, months: tuple[str, ...]) -> dict[str, int]:
    """Taxi-shaped data with the defects profiled in the real feed, plus the ones a CSV adds.

    Returns how many of each defect were injected, so tests can demand that ingest finds
    exactly that many.
    """
    rng = np.random.default_rng(seed)
    starts = np.array([np.datetime64(f"{m}-01", "s") for m in months])
    ends = np.array([np.datetime64(f"{m}-01", "M") + 1 for m in months]).astype("datetime64[s]")
    month = np.sort(rng.integers(0, len(months), rows))  # files arrive month by month...
    hour_weights = np.array([2, 1, 1, 1, 1, 2, 4, 6, 7, 7, 7, 7, 8, 8, 8, 8, 9, 10, 11, 10, 8, 7, 5, 3], float)
    span = (ends - starts).astype(int)[month]
    day = rng.integers(0, span // 86400)
    hour = rng.choice(24, rows, p=hour_weights / hour_weights.sum())
    pickup = starts[month] + (day * 86400 + hour * 3600 + rng.integers(0, 3600, rows)).astype("timedelta64[s]")
    dist = np.round(rng.lognormal(0.6, 0.9, rows), 2)  # ...but not sorted within the month
    seconds = (dist / 11 * 3600 * rng.uniform(0.6, 1.8, rows) + 120).astype(int)
    dropoff = pickup + seconds.astype("timedelta64[s]")
    zones = np.random.default_rng(0).permutation(265) + 1  # a fixed popularity order, skewed like real pickups
    pu, do = zones[rng.zipf(1.4, rows) % 265], zones[rng.zipf(1.3, rows) % 265]
    payment = rng.choice([1, 2, 3, 4], rows, p=[0.80, 0.17, 0.01, 0.02])
    fare = np.round(3 + 2.9 * dist + seconds / 60 * 0.6, 1)
    extra = rng.choice([0.0, 1.0, 2.5, 3.5], rows, p=[0.4, 0.3, 0.2, 0.1])
    tip = np.where(payment == 1, np.round(fare * rng.choice([0, 0.15, 0.2, 0.25], rows), 2), 0.0)
    tolls = np.where(rng.random(rows) < 0.06, 6.94, 0.0)
    congestion = np.where(rng.random(rows) < 0.9, 2.5, 0.0)
    airport = np.where(np.isin(pu, zones[:2]), 1.75, 0.0)
    total = np.round(fare + extra + 0.5 + tip + tolls + 1.0 + congestion + airport, 2)
    no_meta = rng.random(rows) < 0.047  # rows that arrive without trip metadata, as in the real feed
    payment = np.where(no_meta, 0, payment)

    def pick(k: int) -> np.ndarray[Any, Any]:
        return rng.choice(rows, k, replace=False)

    injected = {"negative_amount": max(1, rows * 12 // 1000), "pickup_far_outside_file_month": 5,
                "late_arrival": 8, "trip_distance_implausible": 4, "dropoff_before_pickup": 6,
                "duplicate": max(2, rows // 1000), "unparseable": 5}
    neg = pick(injected["negative_amount"])
    fare[neg], total[neg] = -fare[neg], -total[neg]
    # The four defects below go on disjoint rows so each one is counted under exactly one reason.
    bad = pick(injected["pickup_far_outside_file_month"] + injected["late_arrival"]
               + injected["trip_distance_implausible"] + injected["dropoff_before_pickup"])
    far, late, long, back = np.split(bad, np.cumsum([5, 8, 4])[:3])
    shift = pickup[far] - np.datetime64("2009-01-01T00:10:00", "s")
    pickup[far], dropoff[far] = pickup[far] - shift, dropoff[far] - shift
    pickup[late], dropoff[late] = starts[month[late]] - np.timedelta64(600, "s"), starts[month[late]]
    dist[long] = 312722.3
    dropoff[back] = pickup[back] - np.timedelta64(60, "s")

    def nullable(values: np.ndarray[Any, Any], typ: pa.DataType) -> pa.Array:
        return pa.array(values, type=typ, mask=no_meta)

    table = pa.table({
        "VendorID": pa.array(rng.choice([1, 2, 6], rows, p=[0.25, 0.7499, 0.0001]), pa.int32()),
        "tpep_pickup_datetime": pa.array(pickup), "tpep_dropoff_datetime": pa.array(dropoff),
        "passenger_count": nullable(rng.choice([0, 1, 2, 3, 4, 5, 6], rows, p=[.01, .74, .14, .04, .03, .02, .02]),
                                    pa.int64()),
        "trip_distance": dist,
        "RatecodeID": nullable(rng.choice([1, 2, 5, 99], rows, p=[0.93, 0.04, 0.02, 0.01]), pa.int64()),
        "store_and_fwd_flag": nullable(np.where(rng.random(rows) < 0.005, "Y", "N"), pa.string()),
        "PULocationID": pa.array(pu, pa.int32()), "DOLocationID": pa.array(do, pa.int32()),
        "payment_type": pa.array(payment, pa.int64()),
        "fare_amount": fare, "extra": extra, "mta_tax": np.full(rows, 0.5), "tip_amount": tip,
        "tolls_amount": tolls, "improvement_surcharge": np.full(rows, 1.0), "total_amount": total,
        "congestion_surcharge": nullable(congestion, pa.float64()), "Airport_fee": nullable(airport, pa.float64()),
        "source_month": pa.array(np.array(months)[month]),
    })
    clean_rows = np.setdiff1d(np.arange(rows), bad)  # replay only valid rows, so every copy counts as `duplicate`
    replayed = table.take(rng.choice(clean_rows, injected["duplicate"], replace=False))  # an upstream batch sent twice
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    pacsv.write_csv(pa.concat_tables([table, replayed]), tmp)  # type: ignore[attr-defined]
    m = months[0]
    with tmp.open("a", encoding="utf-8", newline="") as f:  # what a hand-edited or truncated export looks like
        us = f"01/15/{m[:4]}"
        f.write(f'2,{us} 08:30:00 AM,{us} 08:45:00 AM,1,2.1,1,N,132,48,1,14.2,0,0.5,2,0,1,20.2,2.5,0,"{m}"\n')
        f.write(f'1,{m}-03 10:00:00,{m}-03 10:20:00,1,far,1,N,132,48,1,14.2,0,0.5,2,0,1,20.2,2.5,0,"{m}"\n')
        f.write(f'1,{m}-03 11:00:00,{m}-03 11:20:00,one,3.0,1,N,132,48,1,14.2,0,0.5,2,0,1,20.2,2.5,0,"{m}"\n')
        f.write(f"2,{m}-04 09:00:00,{m}-04 09:10:00,1,1.0\n")
        f.write(f'1,{m}-05 09:00:00,{m}-05 09:10:00,1,1.0,1,N,132,48,1,9,0,0.5,0,0,1,13,2.5,0,"{m}",surplus\n')
    os.replace(tmp, path)
    return injected


def land(s: Settings, info: dict[str, Any]) -> None:
    """Produce landing/trips.csv from whichever source is configured. Downstream cannot tell which."""
    out = s.data_dir / "landing" / "trips.csv"
    if s.source == "synthetic":
        fp = {"source": "synthetic", "rows": s.synth_rows, "seed": s.seed, "months": list(s.months)}
    else:
        manifest = read_json(s.data_dir / "raw" / "MANIFEST.json")
        if manifest is None:
            raise DataError("No raw files. Run `bakeoff fetch` first, or set LAKE_SOURCE=synthetic.")
        files = [f"yellow_tripdata_{m}.parquet" for m in s.months]
        fp = {"source": "tlc", "files": {n: manifest[n]["sha256"] for n in files}}
    meta = read_json(out.with_name("landing.json"), {})
    if meta.get("fingerprint") == fp and out.exists():
        info.update(skipped=True, rows=meta["lines"], bytes=out.stat().st_size)
        return
    injected: dict[str, int] = {}
    if s.source == "synthetic":
        injected = synth_taxi(out, s.synth_rows, s.seed, s.months)
    else:
        con = connect(s)
        headers = [c[1] for c in load_toml("taxi.toml")["dataset"]["columns"][:-1]]
        parts = []
        for i, month in enumerate(s.months):  # month strings are regex-validated in Settings
            con.read_parquet(str(s.data_dir / "raw" / f"yellow_tripdata_{month}.parquet")).create_view(f"raw_{i}")
            cols = ", ".join(f"{ident(h)} AS {ident(h)}" for h in headers)
            parts.append(f"SELECT {cols}, '{month}' AS source_month FROM raw_{i}")  # noqa: S608
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        con.sql(" UNION ALL ".join(parts)).write_csv(str(tmp), header=True)
        os.replace(tmp, out)
    lines = count_lines(out) - 1
    write_json(out.with_name("landing.json"), {"fingerprint": fp, "lines": lines, "injected": injected})
    info.update(rows=lines, bytes=out.stat().st_size)


def count_lines(path: Path) -> int:
    with path.open("rb") as f:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 22), b""))


# ---------------------------------------------------------------- dataset contract

def taxi_dataset(s: Settings) -> tuple[Path, dict[str, Any]]:
    return s.data_dir / "landing" / "trips.csv", load_toml("taxi.toml")


def generic_dataset(csv: Path, sort_key: str | None) -> tuple[Path, dict[str, Any]]:
    """Contract for a CSV we know nothing about: sniffed types, no rules, a four-query workload."""
    if not csv.is_file():
        raise DataError(f"--csv {csv} does not exist")
    schema = [(str(r[0]), str(r[1])) for r in duckdb.connect().execute(  # sniff only; touches a sample
        "SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM read_csv(?, header = true))",
        [str(csv)]).fetchall()]
    names = [n for n, _ in schema]
    key = sort_key or names[0]
    if key not in names:
        raise DataError(f"--sort-key {key!r} is not a column of {csv}. Columns: {', '.join(names)}")
    numeric = next((n for n, t in schema if n != key and t.split("(")[0] in
                    ("BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "HUGEINT", "SMALLINT", "TINYINT")), key)
    k, c = ident(key), ident(numeric)
    proj = f"SELECT min({k}), max({c}) FROM t"  # noqa: S608
    return csv, {
        "dataset": {"name": csv.stem, "description": f"User-supplied {csv.name}", "sort_key": key,
                    "columns": [[n, n, t, True, "", ""] for n, t in schema]},
        "rules": [],
        "queries": [
            {"id": "count", "class": "metadata", "sql": "SELECT count(*) FROM t", "description": "Row count."},
            {"id": "full_scan", "class": "full_scan", "sql": "SELECT max(COLUMNS(*)) FROM t",
             "description": "Touches every column."},
            {"id": "projection", "class": "projection", "sql": proj,
             "description": f"Two columns ({key}, {numeric}), no filter."},
            {"id": "filter_clustered", "class": "filter_clustered", "control": "projection",
             "sql": f"{proj} WHERE {k} >= $lo AND {k} < $hi", "description": "1% range filter on the sort key."},
        ],
    }


# ---------------------------------------------------------------- profile

def profile(s: Settings, landing: Path, info: dict[str, Any]) -> None:
    """Look at the data before trusting it: types as sniffed, nulls, cardinality, ranges."""
    con = connect(s)
    rows = con.execute(
        "SELECT column_name, column_type, min, max, approx_unique, null_percentage, count "
        "FROM (SUMMARIZE SELECT * FROM read_csv(?, header = true, ignore_errors = true))", [str(landing)]).fetchall()
    cols = [{"column": r[0], "sniffed_type": r[1], "min": r[2], "max": r[3], "approx_distinct": r[4],
             "null_pct": float(r[5]), "rows": r[6]} for r in rows]
    write_json(s.data_dir / "landing" / "profile.json", {"file": landing.name, "bytes": landing.stat().st_size,
                                                         "columns": cols})
    info.update(rows=cols[0]["rows"] if cols else 0, bytes=landing.stat().st_size)


# ---------------------------------------------------------------- ingest

def ingest(s: Settings, landing: Path, ds: dict[str, Any], info: dict[str, Any]) -> None:
    """Validate at the boundary. Every landed line ends up in clean/ or in quarantine/ with a reason."""
    clean = s.data_dir / "clean" / "source.parquet"
    rejects = s.data_dir / "quarantine" / "rejects.parquet"
    if not landing.exists():
        raise DataError(f"{landing} does not exist. Run `bakeoff land` first.")
    fp = hashlib.sha256((sha256(landing) + json.dumps(ds, sort_keys=True)).encode()).hexdigest()
    done = read_json(clean.with_name("ingest.json"), {})
    if done.get("fingerprint") == fp and clean.exists() and rejects.exists():
        info.update(skipped=True, rows=done["landed"], bytes=landing.stat().st_size)
        return

    cols = ds["dataset"]["columns"]
    names = [ident(c[0]) for c in cols]
    key = ident(ds["dataset"]["sort_key"])
    types = "{" + ", ".join(f"'{c[1]}': '{c[2]}'" for c in cols) + "}"
    rules = [{"id": "null_in_required", "quarantine": True,
              "when": " OR ".join(f"{ident(c[0])} IS NULL" for c in cols if not c[3]) or "false"}, *ds["rules"]]
    reason = " ".join(f"WHEN {r['when']} THEN '{r['id']}'" for r in rules if r["quarantine"])
    con = connect(s)
    try:
        select = ", ".join(f"{ident(c[1])} AS {ident(c[0])}" for c in cols)
        con.execute(f"CREATE TABLE parsed AS SELECT {select} FROM read_csv(?, header = true, types = {types}, "  # noqa: S608
                    "store_rejects = true, rejects_table = 'rej', rejects_scan = 'rej_scan')", [str(landing)])
    except duckdb.Error as e:
        raise DataError(f"{landing.name} does not match the dataset contract: {e}") from e
    con.execute(f"CREATE TABLE judged AS SELECT *, CASE {reason} END AS _reason FROM parsed")  # noqa: S608
    con.execute(f"CREATE TABLE dedup AS SELECT {', '.join(names)}, count(*) AS _n FROM judged "  # noqa: S608
                "WHERE _reason IS NULL GROUP BY ALL")
    for path in (clean, rejects):
        path.parent.mkdir(parents=True, exist_ok=True)
    # Total order (sort key, then every column) makes the output independent of thread scheduling.
    tmp = clean.with_name("source.tmp")
    con.execute(f"COPY (SELECT {', '.join(names)} FROM dedup ORDER BY {key}, {', '.join(names)}) TO '{tmp.as_posix()}' "  # noqa: S608
                "(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)")
    rtmp = rejects.with_name("rejects.tmp")
    con.execute(f"""COPY (
        SELECT _reason AS reason, 1 AS copies, NULL::BIGINT AS line, to_json(j)::VARCHAR AS raw FROM judged j
        WHERE _reason IS NOT NULL
        UNION ALL SELECT 'duplicate', _n - 1, NULL, to_json(d)::VARCHAR FROM dedup d WHERE _n > 1
        UNION ALL SELECT 'unparseable', 1, line, any_value(csv_line) || '  <- ' || string_agg(error_message, '; ')
        FROM rej GROUP BY line ORDER BY reason, raw
    ) TO '{rtmp.as_posix()}' (FORMAT parquet)""")  # noqa: S608
    os.replace(tmp, clean)
    os.replace(rtmp, rejects)

    landed = count_lines(landing) - 1
    n_clean = con.execute("SELECT count(*) FROM dedup").fetchone()[0]  # type: ignore[index]
    quarantined = dict(con.execute("SELECT reason, sum(copies)::BIGINT FROM read_parquet(?) GROUP BY 1 ORDER BY 1",
                                   [str(rejects)]).fetchall())
    if landed != n_clean + sum(quarantined.values()):
        raise DataError(f"Row accounting failed: {landed} landed != {n_clean} clean + {sum(quarantined.values())} "
                        "quarantined. Rows were lost or invented; refusing to publish this ingest.")
    warn = [r for r in rules if not r["quarantine"]]
    counts = con.execute("SELECT " + (", ".join(f"count(*) FILTER (WHERE {r['when']})" for r in warn) or "0")  # noqa: S608
                         + " FROM dedup").fetchone() if warn else ()
    lo, hi = con.execute(f"SELECT quantile_disc({key}, 0.50), quantile_disc({key}, 0.51) FROM dedup").fetchone()  # type: ignore[misc]  # noqa: S608
    schema = con.execute("SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM read_parquet(?))",
                         [str(clean)]).fetchall()
    write_json(clean.with_name("ingest.json"), {
        "fingerprint": fp, "dataset": ds["dataset"]["name"], "sort_key": ds["dataset"]["sort_key"],
        "landed": landed, "landing_bytes": landing.stat().st_size, "clean": n_clean,
        "quarantined": quarantined, "warnings": dict(zip([r["id"] for r in warn], counts or (), strict=False)),
        "schema": [list(r) for r in schema], "window": {"lo": lo, "hi": hi},
    })
    info.update(rows=landed, bytes=landing.stat().st_size, clean=n_clean, quarantined=sum(quarantined.values()))


def load_clean(s: Settings) -> tuple[pa.Table, dict[str, Any]]:
    meta = read_json(s.data_dir / "clean" / "ingest.json")
    if meta is None:
        raise DataError("No ingested data. Run `bakeoff ingest` first.")
    return pq.read_table(s.data_dir / "clean" / "source.parquet"), meta
