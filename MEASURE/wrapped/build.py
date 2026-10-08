"""Batch generation (one payload per user) and the audit that checks every payload against the warehouse."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb

from wrapped import cards
from wrapped.cards import Rank
from wrapped.config import Settings, log

logger = logging.getLogger(__name__)
RUN_NAMESPACE = uuid.UUID("5f0c1d7e-6c5b-4f6e-9a54-0d7f3f1a2b16")
CHUNK = 5_000


def _connect(settings: Settings) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(
        str(settings.warehouse_path),
        read_only=True,
        config={
            "memory_limit": settings.duckdb_memory,
            "threads": settings.duckdb_threads,
            "temp_directory": str(settings.data_dir / "tmp"),
        },
    )


def _dicts(cur: duckdb.DuckDBPyConnection) -> Iterator[dict[str, Any]]:
    names = [d[0] for d in cur.description]
    while rows := cur.fetchmany(CHUNK):
        for r in rows:
            yield dict(zip(names, r, strict=True))


def _json_default(value: object) -> str:
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=_json_default, separators=(",", ":"), ensure_ascii=False)


def _population(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    return next(_dicts(con.execute("SELECT * FROM mart_population")))


def run_identity(settings: Settings, con: duckdb.DuckDBPyConnection) -> tuple[uuid.UUID, str]:
    """A run is identified by what it was built from: the same raw files and catalogue give the same run id."""
    row = con.execute(
        "SELECT md5(string_agg(source_file || ':' || size_bytes::VARCHAR, '|' ORDER BY source_file, size_bytes)) "
        "FROM read_parquet(?)",
        [str(settings.data_dir / "manifest" / "*.parquet")],
    ).fetchone()
    fingerprint = str(row[0]) if row and row[0] else "empty"
    name = f"{settings.year}:{fingerprint}:catalogue-{cards.CATALOGUE_VERSION}:k-{settings.k_anonymity}"
    return uuid.uuid5(RUN_NAMESPACE, name), fingerprint


# Per-user inputs are read as sorted streams and merged here, never joined or list-aggregated in the
# database: memory stays flat however many users there are, which is what lets 200,000 users run in 1 GB.
_USERS = "SELECT * FROM mart_user_year ORDER BY user_id"
_RANKS = "SELECT user_id, metric, users_at_or_above, population FROM mart_user_ranks ORDER BY user_id"


def _by_user(con: duckdb.DuckDBPyConnection, sql: str) -> Callable[[int], list[tuple[Any, ...]]]:
    """Turn a query sorted by its first column (user_id) into `take(user_id)`, for ids asked in increasing order."""
    cur = con.cursor().execute(sql)
    buffer: list[tuple[Any, ...]] = []
    position = 0

    def take(user_id: int) -> list[tuple[Any, ...]]:
        nonlocal buffer, position
        out: list[tuple[Any, ...]] = []
        while True:
            if position == len(buffer):
                buffer, position = cur.fetchmany(CHUNK * 4), 0
                if not buffer:
                    return out
            row = buffer[position]
            if row[0] > user_id:
                return out
            if row[0] == user_id:
                out.append(row[1:])
            position += 1

    return take


def _ranks(rows: list[tuple[Any, ...]]) -> dict[str, Rank]:
    return {metric: Rank(int(at_or_above), int(population)) for metric, at_or_above, population in rows}


def build(settings: Settings) -> dict[str, Any]:
    """Generate every user's payload into `payloads/<run_id>/payloads.parquet`. The same inputs are a no-op."""
    started = time.perf_counter()
    con = _connect(settings)
    try:
        run_id, fingerprint = run_identity(settings, con)
        out_dir = settings.payload_dir / str(run_id)
        summary_path = out_dir / "run.json"
        if summary_path.exists():
            summary: dict[str, Any] = json.loads(summary_path.read_text())
            log(logger, "build skipped: run already built", run_id=str(run_id))
            return summary | {"reused": True}

        shutil.rmtree(out_dir, ignore_errors=True)  # a crashed build left no run.json; start clean
        out_dir.mkdir(parents=True)
        population = _population(con)
        tiers: dict[str, int] = {}
        users = 0
        ndjson = out_dir / "payloads.ndjson.gz"
        with gzip.open(ndjson, "wt", encoding="utf-8", compresslevel=1) as out:
            ranks_of = _by_user(con, _RANKS)
            for row in _dicts(con.cursor().execute(_USERS)):
                payload = cards.build_payload(row, _ranks(ranks_of(row["user_id"])), population)
                tiers[payload["tier"]] = tiers.get(payload["tier"], 0) + 1
                users += 1
                record = {
                    "user_id": row["user_id"],
                    "login": row["login"],
                    "tier": payload["tier"],
                    "archetype": payload["archetype"],
                    "card_types": [c["type"] for c in payload["cards"]],
                    "payload": dumps(payload),
                }
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        con.close()

    # Columnar copy for the audit, the distribution report and the load into Postgres.
    scratch = duckdb.connect(config={"memory_limit": settings.duckdb_memory, "threads": settings.duckdb_threads})
    try:
        columns = "{'user_id': 'BIGINT', 'login': 'VARCHAR', 'tier': 'VARCHAR', 'archetype': 'VARCHAR', "
        columns += "'card_types': 'VARCHAR[]', 'payload': 'VARCHAR'}"
        scratch.execute(
            f"COPY (SELECT * FROM read_json(?, format = 'newline_delimited', columns = {columns}) ORDER BY user_id) "
            f"TO '{(out_dir / 'payloads.parquet').as_posix()}' "
            "(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 20000)",
            [str(ndjson)],
        )
        distribution = superlative_distribution(scratch, out_dir / "payloads.parquet")
    finally:
        scratch.close()
    ndjson.unlink()

    seconds = time.perf_counter() - started
    summary = {
        "run_id": str(run_id),
        "year": settings.year,
        "source_fingerprint": fingerprint,
        "population": int(population["users"]),
        "users": users,
        "tiers": tiers,
        "seconds": round(seconds, 2),
        "users_per_second": round(users / seconds) if seconds else users,
        "distribution": distribution,
    }
    summary_path.write_text(json.dumps(summary, indent=2))  # written last: marks the run as complete
    log(logger, "build finished", **{k: v for k, v in summary.items() if k != "distribution"})
    return summary


def superlative_distribution(con: duckdb.DuckDBPyConnection, payloads: Path) -> dict[str, Any]:
    """How the selectable cards are spread across users, and how often the single most common story occurs."""
    path = payloads.as_posix()
    total = con.execute("SELECT count(*) FROM read_parquet(?)", [path]).fetchone()
    assert total is not None
    by_type = con.execute(
        "SELECT card_type, count(*) AS users FROM (SELECT unnest(card_types) AS card_type FROM read_parquet(?)) "
        "WHERE card_type NOT IN ('intro', 'summary') GROUP BY card_type ORDER BY users DESC, card_type",
        [path],
    ).fetchall()
    combos = con.execute(
        "SELECT count(*) AS distinct_stories, max(users) AS most_common FROM ("
        "  SELECT list_sort(card_types) AS story, count(*) AS users FROM read_parquet(?) GROUP BY story)",
        [path],
    ).fetchone()
    assert combos is not None
    return {
        "users": total[0],
        "card_share": {name: round(n / total[0], 4) for name, n in by_type} if total[0] else {},
        "distinct_stories": combos[0],
        "most_common_story_share": round((combos[1] or 0) / total[0], 4) if total[0] else 0.0,
    }


# The expensive, exact standing: a full sort per metric. Verification only; the pipeline never does this.
_EXACT = """
SELECT user_id, metric, at_or_above FROM (
    SELECT user_id, metric, count(*) OVER (PARTITION BY metric ORDER BY value DESC) AS at_or_above
    FROM mart_metric_values
) ORDER BY user_id
"""


def _stored_payloads(con: duckdb.DuckDBPyConnection, parquet: Path) -> Iterator[tuple[int, str]]:
    """Payloads in file order, which `build` wrote sorted by user id. Streamed: the payload text of a whole
    population does not fit in a join's hash table on a small machine, so the audit merges two sorted streams."""
    con.execute("SET preserve_insertion_order = true")
    cur = con.cursor().execute("SELECT user_id, payload FROM read_parquet(?)", [str(parquet)])
    last = -1
    while rows := cur.fetchmany(CHUNK):
        for user_id, payload in rows:
            if user_id <= last:
                raise RuntimeError(f"{parquet} is not sorted by user_id; rebuild the run")
            last = user_id
            yield user_id, payload


def audit(settings: Settings, max_reported: int = 20) -> dict[str, Any]:
    """Check every stored payload against the warehouse. Returns the violations; an empty list is the pass.

    Per payload: (1) it is byte-for-byte what the selector produces from today's warehouse; (2) every
    number in every card's facts equals the warehouse column of the same name; (3) every "top X%" holds
    against an exact rank recomputed by sorting; (4) every comparison rests on a group of at least k.
    """
    started = time.perf_counter()
    con = _connect(settings)
    violations: list[dict[str, Any]] = []
    payloads = claims = 0

    def fail(user_id: object, rule: str, detail: str) -> None:
        violations.append({"user_id": user_id, "rule": rule, "detail": detail})

    try:
        run_id, _ = run_identity(settings, con)
        population = _population(con)
        pop_exact = con.execute("SELECT count(*), count(weekend_share) FROM mart_user_year").fetchone()
        assert pop_exact is not None
        parquet = settings.payload_dir / str(run_id) / "payloads.parquet"
        stored_stream = _stored_payloads(con, parquet)
        pending = next(stored_stream, None)
        ranks_of, exact_of = _by_user(con, _RANKS), _by_user(con, _EXACT)
        for row in _dicts(con.cursor().execute(_USERS)):
            ranks = _ranks(ranks_of(row["user_id"]))
            exact = {metric: int(at_or_above) for metric, at_or_above in exact_of(row["user_id"])}
            while pending is not None and pending[0] < row["user_id"]:
                fail(pending[0], "coverage", "payload without user")
                pending = next(stored_stream, None)
            if pending is None or pending[0] != row["user_id"]:
                fail(row["user_id"], "coverage", "user without payload")
                continue
            stored = pending[1]
            pending = next(stored_stream, None)
            payloads += 1
            if dumps(cards.build_payload(row, ranks, population)) != stored:
                fail(row["user_id"], "reproducible", "stored payload differs from a rebuild")
            for card in json.loads(stored)["cards"]:
                for name, value in card["facts"].items():
                    source = (
                        population.get(name.removeprefix("population_"))
                        if name.startswith("population_")
                        else row[name]
                    )
                    if isinstance(source, dt.date | dt.datetime):
                        source = source.isoformat()
                    if source != value:
                        fail(
                            row["user_id"],
                            "fact",
                            f"{card['type']}.{name}: card says {value!r}, warehouse says {source!r}",
                        )
                if claim := card["claim"]:
                    claims += 1
                    metric = claim["metric"]
                    true_population = pop_exact[1] if metric == "weekend_share" else pop_exact[0]
                    if metric not in exact or exact[metric] * 1000 > claim["top_permille"] * true_population:
                        fail(row["user_id"], "claim", f"{card['type']}: {claim['text']} but {exact.get(metric)} of "
                             f"{true_population} are at or above")  # fmt: skip
                    if ranks[metric].users_at_or_above < settings.k_anonymity:
                        fail(row["user_id"], "k-anonymity", f"{metric}: group of {ranks[metric].users_at_or_above}")
        while pending is not None:
            fail(pending[0], "coverage", "payload without user")
            pending = next(stored_stream, None)
    finally:
        con.close()
    result = {
        "run_id": str(run_id),
        "payloads": payloads,
        "claims": claims,
        "violation_count": len(violations),
        "violations": violations[:max_reported],
        "seconds": round(time.perf_counter() - started, 2),
    }
    log(logger, "audit finished", **{k: v for k, v in result.items() if k != "violations"})
    return result
