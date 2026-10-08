"""SQLite storage: schema, migrations, ingest and the queries the API serves.

The database is derived. Its sources are the files in results/raw (pipeline run records, device
results, package records), and `python -m squeeze ingest` rebuilds it from them at any time.
Every statement is parameterized.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import statistics
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .schemas import BenchResult

MIGRATIONS = [
    """
    CREATE TABLE run (
        run_id            TEXT PRIMARY KEY,
        created_at        TEXT NOT NULL,
        git_commit        TEXT NOT NULL,
        dataset_version   TEXT NOT NULL,
        teacher_top1      REAL NOT NULL,
        budget_pt         REAL NOT NULL,
        config            TEXT NOT NULL,
        baselines         TEXT NOT NULL,
        calibration_study TEXT NOT NULL
    );
    CREATE TABLE variant (
        run_id          TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
        name            TEXT NOT NULL,
        parent          TEXT,
        technique       TEXT NOT NULL,
        arch            TEXT NOT NULL,
        precision       TEXT NOT NULL CHECK (precision IN ('fp32', 'int8', 'int8+fp32')),
        params          INTEGER NOT NULL CHECK (params > 0),
        size_bytes      INTEGER NOT NULL CHECK (size_bytes > 0),
        model_sha256    TEXT NOT NULL CHECK (length(model_sha256) = 64),
        top1            REAL NOT NULL CHECK (top1 BETWEEN 0 AND 1),
        top1_lo         REAL NOT NULL,
        top1_hi         REAL NOT NULL,
        n_eval          INTEGER NOT NULL CHECK (n_eval > 0),
        top1_deploy     REAL NOT NULL CHECK (top1_deploy BETWEEN 0 AND 1),
        parent_agree    REAL,
        parent_delta    REAL,
        parent_delta_lo REAL,
        parent_delta_hi REAL,
        gate            TEXT NOT NULL CHECK (gate IN ('pass', 'fail')),
        gate_reason     TEXT NOT NULL,
        detail          TEXT NOT NULL,
        position        INTEGER NOT NULL,
        PRIMARY KEY (run_id, name)
    );
    CREATE TABLE sensitivity (
        run_id     TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
        variant    TEXT NOT NULL,
        node       TEXT NOT NULL,
        op_type    TEXT NOT NULL,
        rank       INTEGER NOT NULL,
        kl         REAL NOT NULL,
        top1_drop  REAL NOT NULL,
        kept_float INTEGER NOT NULL CHECK (kept_float IN (0, 1)),
        PRIMARY KEY (run_id, variant, node)
    );
    CREATE TABLE bench (
        result_id         TEXT PRIMARY KEY,
        body_sha256       TEXT NOT NULL,
        received_at       TEXT NOT NULL,
        measured_at       TEXT NOT NULL,
        source            TEXT NOT NULL,
        target            TEXT NOT NULL,
        variant           TEXT NOT NULL,
        model_sha256      TEXT NOT NULL,
        runtime           TEXT NOT NULL,
        runtime_version   TEXT NOT NULL,
        provider          TEXT NOT NULL,
        threads           INTEGER NOT NULL,
        machine           TEXT NOT NULL,
        cpu_model         TEXT NOT NULL,
        cores             INTEGER NOT NULL,
        os                TEXT NOT NULL,
        board             TEXT,
        n                 INTEGER NOT NULL,
        p50_ms            REAL NOT NULL CHECK (p50_ms > 0),
        p95_ms            REAL NOT NULL,
        p99_ms            REAL NOT NULL,
        mean_ms           REAL NOT NULL,
        n_acc             INTEGER NOT NULL,
        top1_device       REAL NOT NULL,
        agree_host        REAL NOT NULL,
        idle_w            REAL,
        load_w            REAL,
        energy_mj         REAL,
        power_source      TEXT,
        power_unavailable TEXT,
        synthetic         INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0, 1))
    );
    -- The explorer reads one target and runtime at a time.
    CREATE INDEX bench_by_target ON bench (target, runtime, synthetic, variant);
    -- Keyset pagination of the result log, newest first.
    CREATE INDEX bench_by_arrival ON bench (received_at DESC, result_id DESC);
    CREATE TABLE package (
        package_id      TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        target          TEXT NOT NULL,
        run_id          TEXT NOT NULL,
        filename        TEXT NOT NULL,
        size_bytes      INTEGER NOT NULL,
        git_commit      TEXT NOT NULL,
        dataset_version TEXT NOT NULL,
        variants        TEXT NOT NULL
    );
    CREATE INDEX package_by_target ON package (target, name);
    CREATE TABLE quarantine (
        id          INTEGER PRIMARY KEY,
        body_sha256 TEXT NOT NULL UNIQUE,
        received_at TEXT NOT NULL,
        source      TEXT NOT NULL,
        errors      TEXT NOT NULL,
        body        TEXT NOT NULL
    );
    """,
]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def connect(path: Path) -> sqlite3.Connection:
    # One connection per request, never shared; FastAPI may open and use it on different threads.
    con = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def write(con: sqlite3.Connection) -> Iterator[None]:
    """One writer at a time, all or nothing."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def migrate(con: sqlite3.Connection) -> None:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        con.executescript(f"BEGIN IMMEDIATE; {script}; PRAGMA user_version = {i}; COMMIT;")


# --- ingest -----------------------------------------------------------------------------------


def canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _quarantine(con: sqlite3.Connection, body: str, source: str, errors: list[Any]) -> None:
    with write(con):
        con.execute(
            "INSERT OR IGNORE INTO quarantine (body_sha256, received_at, source, errors, body) VALUES (?, ?, ?, ?, ?)",
            (hashlib.sha256(body.encode()).hexdigest(), now(), source, json.dumps(errors), body[:20_000]),
        )


def ingest_bench(
    con: sqlite3.Connection, payload: Any, source: str, known_targets: set[str]
) -> tuple[str, list[Any], str | None]:
    """Validate and store one device result. Returns (status, errors, result_id) where status is
    created | duplicate | conflict | quarantined. Safe to call again with the same payload."""
    errors: list[Any] = []
    result: BenchResult | None = None
    body = payload if isinstance(payload, str) else canonical(payload)
    try:
        if isinstance(payload, str):
            payload = json.loads(payload)
            body = canonical(payload)  # the same result hashes the same however it was serialised
        result = BenchResult.model_validate(payload)
        if result.target not in known_targets:
            errors = [{"loc": ["target"], "msg": f"unknown target; known: {sorted(known_targets)}"}]
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    except (ValueError, TypeError) as exc:
        errors = [{"loc": [], "msg": f"not JSON: {exc}"}]
    if errors or result is None:
        _quarantine(con, body, source, errors)
        return "quarantined", errors, None

    digest = hashlib.sha256(body.encode()).hexdigest()
    with write(con):
        seen = con.execute("SELECT body_sha256 FROM bench WHERE result_id = ?", (result.result_id,)).fetchone()
        if seen:
            return ("duplicate" if seen[0] == digest else "conflict"), [], result.result_id
        p, d, lat, acc = result.power, result.device, result.latency_ms, result.accuracy
        con.execute(
            "INSERT INTO bench VALUES (" + ",".join("?" * 31) + ")",  # noqa: S608 - placeholders only
            (
                result.result_id, digest, now(), result.measured_at.isoformat(timespec="seconds"), source,
                result.target, result.variant, result.model_sha256, result.runtime, result.runtime_version,
                result.provider, result.threads, d.machine, d.cpu_model, d.cores, d.os, d.board,
                lat.n, lat.p50, lat.p95, lat.p99, lat.mean, acc.n, acc.top1, acc.agree_host,
                p.idle_w if p else None, p.load_w if p else None, p.energy_mj if p else None,
                p.source if p else None, result.power_unavailable, int(result.synthetic),
            ),
        )  # fmt: skip
    return "created", [], result.result_id


def ingest_pipeline(con: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Replace everything known about this run with the record. Re-ingesting is a no-op."""
    run_id = record["run_id"]
    teacher = next(v for v in record["variants"] if v["parent"] is None and v["technique"] == "none")
    with write(con):
        con.execute("DELETE FROM run WHERE run_id = ?", (run_id,))
        con.execute(
            "INSERT INTO run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, record["created_at"], record["git_commit"], record["dataset"]["version"],
                teacher["eval"]["top1"], record["config"]["budget_pt"], json.dumps(record["config"]),
                json.dumps(record["baselines"]), json.dumps(record["calibration_study"]),
            ),
        )  # fmt: skip
        for position, v in enumerate(record["variants"]):
            vs = v["vs_parent"] or {}
            con.execute(
                "INSERT INTO variant VALUES (" + ",".join("?" * 22) + ")",  # noqa: S608 - placeholders only
                (
                    run_id, v["name"], v["parent"], v["technique"], v["arch"], v["precision"], v["params"],
                    v["size_bytes"], v["sha256"], v["eval"]["top1"], v["eval"]["lo"], v["eval"]["hi"],
                    v["eval"]["n"], v["eval_deploy"]["top1"], vs.get("agree"), vs.get("delta"), vs.get("lo"),
                    vs.get("hi"), v["gate"], v["gate_reason"], json.dumps(v["detail"]), position,
                ),
            )  # fmt: skip
        sens = record["sensitivity"]
        con.executemany(
            "INSERT INTO sensitivity VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    sens["variant"],
                    r["node"],
                    r["op_type"],
                    r["rank"],
                    r["kl"],
                    r["top1_drop"],
                    int(r["kept_float"]),
                )
                for r in sens["rows"]
            ],
        )


def ingest_package(con: sqlite3.Connection, record: dict[str, Any]) -> None:
    m = record["manifest"]
    with write(con):
        con.execute(
            "INSERT OR REPLACE INTO package VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["package_id"], m["name"], m["target"]["name"], m["run_id"], record["filename"],
                record["size_bytes"], m["git_commit"], m["dataset_version"], json.dumps(m["variants"]),
            ),
        )  # fmt: skip


def ingest_dir(con: sqlite3.Connection, raw_dir: Path, known_targets: set[str]) -> dict[str, int]:
    """Load every raw file: *.json holds one record, *.jsonl one device result per line. Order
    does not matter: results that arrive before their pipeline run are stored and join up later."""
    counts: dict[str, int] = {}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    for path in sorted(raw_dir.glob("*.json*")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines() if path.suffix == ".jsonl" else [text]:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                kind = record.get("kind") if isinstance(record, dict) else None
            except ValueError:
                kind = None
            if kind == "pipeline":
                ingest_pipeline(con, record)
                bump("pipeline")
            elif kind == "package":
                ingest_package(con, record)
                bump("package")
            else:
                bump(ingest_bench(con, line, path.name, known_targets)[0])
    return counts


# --- queries ----------------------------------------------------------------------------------


def encode_cursor(*parts: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode()


def decode_cursor(cursor: str, n: int) -> list[Any]:
    parts = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    if not isinstance(parts, list) or len(parts) != n:
        raise ValueError("wrong shape")
    return parts


def latest_run(con: sqlite3.Connection) -> sqlite3.Row | None:
    row: sqlite3.Row | None = con.execute("SELECT * FROM run ORDER BY created_at DESC, run_id LIMIT 1").fetchone()
    return row


def pareto(points: list[tuple[float, float]]) -> list[bool]:
    """points are (latency, accuracy). True where no other point is at least as good on both and
    better on one."""
    return [not any(o[0] <= p[0] and o[1] >= p[1] and (o[0] < p[0] or o[1] > p[1]) for o in points) for p in points]


def tradeoff(con: sqlite3.Connection, target: dict[str, Any], runtime: str, synthetic: bool = False) -> dict[str, Any]:
    run = latest_run(con)
    measured = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT runtime FROM bench WHERE target = ? AND synthetic = ? ORDER BY runtime",
            (target["name"], int(synthetic)),
        )
    ]
    out: dict[str, Any] = {
        "target": target, "runtime": runtime, "runtimes_measured": measured, "run": None, "baselines": [], "points": [],
    }  # fmt: skip
    if run is None:
        return out
    out["run"] = {
        k: run[k] for k in ("run_id", "created_at", "git_commit", "dataset_version", "teacher_top1", "budget_pt")
    }
    out["baselines"] = json.loads(run["baselines"])
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in con.execute(
        "SELECT * FROM bench WHERE target = ? AND runtime = ? AND synthetic = ? ORDER BY measured_at",
        (target["name"], runtime, int(synthetic)),
    ):
        groups.setdefault((row["variant"], row["model_sha256"]), []).append(row)

    budget = target.get("budget_p95_ms")
    for v in con.execute("SELECT * FROM variant WHERE run_id = ? ORDER BY position", (run["run_id"],)):
        point = {k: v[k] for k in v.keys() if k not in ("run_id", "detail", "position", "model_sha256")}
        rows = groups.get((v["name"], v["model_sha256"]))
        point["bench"] = None
        point["meets_budget"] = None
        if rows:
            # Several runs of the same model on the same target: the median run speaks for them.
            def med(key: str, rows: list[sqlite3.Row] = rows) -> float | None:
                values = [r[key] for r in rows if r[key] is not None]
                return statistics.median(values) if values else None

            last = rows[-1]
            point["bench"] = {
                "runs": len(rows),
                **{
                    k: med(k)
                    for k in (
                        "p50_ms",
                        "p95_ms",
                        "p99_ms",
                        "top1_device",
                        "agree_host",
                        "load_w",
                        "idle_w",
                        "energy_mj",
                    )
                },
                **{k: last[k] for k in ("power_source", "power_unavailable", "cpu_model", "threads")},
                "last_measured_at": last["measured_at"],
            }
            if budget is not None:
                point["meets_budget"] = point["bench"]["p95_ms"] <= budget
        out["points"].append(point)

    eligible = [p for p in out["points"] if p["bench"] and p["gate"] == "pass"]
    flags = pareto([(p["bench"]["p50_ms"], p["top1"]) for p in eligible])
    for p in out["points"]:
        p["pareto"] = False
    for p, flag in zip(eligible, flags, strict=True):
        p["pareto"] = flag
    return out


def results_page(con: sqlite3.Connection, target: str | None, cursor: str | None, limit: int) -> dict[str, Any]:
    where, args = ["1 = 1"], []
    if target:
        where.append("b.target = ?")
        args.append(target)
    if cursor:
        where.append("(b.received_at, b.result_id) < (?, ?)")
        args += decode_cursor(cursor, 2)
    # `where` holds only the fixed fragments above; every value is bound.
    sql = (
        "SELECT b.*, EXISTS (SELECT 1 FROM variant v WHERE v.model_sha256 = b.model_sha256 AND v.name = b.variant)"  # noqa: S608
        " AS known_model FROM bench b WHERE " + " AND ".join(where) + ""
        " ORDER BY b.received_at DESC, b.result_id DESC LIMIT ?"
    )
    rows = con.execute(sql, (*args, limit + 1)).fetchall()
    more = len(rows) > limit
    items = [dict(r) for r in rows[:limit]]
    return {
        "items": items,
        "next_cursor": encode_cursor(items[-1]["received_at"], items[-1]["result_id"]) if more else None,
    }


def quarantine_page(con: sqlite3.Connection, cursor: str | None, limit: int) -> dict[str, Any]:
    before = decode_cursor(cursor, 1)[0] if cursor else 2**62
    rows = con.execute(
        "SELECT id, received_at, source, errors, body FROM quarantine WHERE id < ? ORDER BY id DESC LIMIT ?",
        (before, limit + 1),
    ).fetchall()
    items = [{**dict(r), "errors": json.loads(r["errors"])} for r in rows[:limit]]
    return {"items": items, "next_cursor": encode_cursor(items[-1]["id"]) if len(rows) > limit else None}


def state_tag(con: sqlite3.Connection) -> str:
    """Changes whenever anything a read endpoint returns could have changed. Used as the ETag."""
    row = con.execute(
        "SELECT (SELECT COUNT(*) || ':' || COALESCE(MAX(received_at), '') FROM bench),"
        " (SELECT COUNT(*) || ':' || COALESCE(MAX(created_at), '') FROM run), (SELECT COUNT(*) FROM package)"
    ).fetchone()
    return hashlib.sha256("|".join(map(str, row)).encode()).hexdigest()[:20]
