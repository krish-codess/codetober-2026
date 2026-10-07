"""Batch generation (one payload per user) and the audit that checks every payload against the warehouse."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import shutil
import time
import uuid
from collections.abc import Iterator
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


_USERS_WITH_RANKS = """
SELECT u.*, r.ranks
FROM mart_user_year u
LEFT JOIN (
    SELECT user_id, list({'metric': metric, 'users_at_or_above': users_at_or_above, 'population': population}) AS ranks
    FROM mart_user_ranks GROUP BY user_id
) r USING (user_id)
"""


def _ranks(row: dict[str, Any]) -> dict[str, Rank]:
    return {r["metric"]: Rank(int(r["users_at_or_above"]), int(r["population"])) for r in row.pop("ranks") or []}


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
            for row in _dicts(con.execute(_USERS_WITH_RANKS)):
                payload = cards.build_payload(row, _ranks(row), population)
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


_AUDIT_SQL = """
WITH exact AS (
    -- The expensive, exact standing: a full sort per metric. Verification only; the pipeline never does this.
    SELECT user_id, metric, count(*) OVER (PARTITION BY metric ORDER BY value DESC) AS at_or_above
    FROM mart_metric_values
), exact_lists AS (
    SELECT user_id, list({'metric': metric, 'at_or_above': at_or_above}) AS exact FROM exact GROUP BY user_id
), published AS (
    SELECT user_id, list({'metric': metric, 'users_at_or_above': users_at_or_above, 'population': population}) AS ranks
    FROM mart_user_ranks GROUP BY user_id
)
SELECT u.*, p.ranks, e.exact, s.payload
FROM mart_user_year u
LEFT JOIN published p USING (user_id)
LEFT JOIN exact_lists e USING (user_id)
FULL JOIN read_parquet(?) s USING (user_id)
"""


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
        for row in _dicts(con.execute(_AUDIT_SQL, [str(parquet)])):
            stored, exact = row.pop("payload"), {e["metric"]: int(e["at_or_above"]) for e in row.pop("exact") or []}
            if stored is None or row["events"] is None:
                fail(row["user_id"], "coverage", "user without payload" if stored is None else "payload without user")
                continue
            payloads += 1
            ranks = _ranks(row)
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
