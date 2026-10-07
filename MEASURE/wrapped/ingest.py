"""Raw hourly files -> validated bronze Parquet, a quarantine, and a manifest.

Raw files are never modified or moved. Each run picks up the files the manifest has not seen
(identity = name + byte size), validates every line, and writes three Parquet files per batch:
accepted events, quarantined lines with a reason, and one manifest row per source file. The
manifest is written last and is the commit marker, so a crash mid-batch leaves nothing that a
re-run would double count.

Works the same on real GH Archive downloads and on the generator's output: both are just
`*.json.gz` in the raw directory.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import duckdb

from wrapped.config import Settings, log, warn

logger = logging.getLogger(__name__)

BATCH_MAX_FILES = 500
BATCH_MAX_BYTES = 256 * 1024 * 1024
QUARANTINE_REASONS = (
    "malformed_json",
    "missing_event_id",
    "missing_actor",
    "missing_type",
    "unparseable_timestamp",
    "timestamp_out_of_range",
)
SEP = b"\x01"  # separates the file index from the line in the staged file; illegal inside JSON

_READ_STAGED = (
    "read_csv(?, columns = {'file_idx': 'INTEGER', 'line': 'VARCHAR'}, delim = chr(1), quote = '', escape = '', "
    "header = false, auto_detect = false, max_line_size = 4194304)"
)

# One pass over the staged lines. A line is a single text column, which keeps malformed lines
# instead of losing them to a parser error.
_PARSE_SQL = f"""
CREATE OR REPLACE TEMP TABLE parsed AS
WITH lines AS (
    -- `doc` is always valid JSON, so the extraction below cannot raise on a broken line.
    SELECT line, source_file, CASE WHEN json_valid(line) THEN line ELSE 'null' END AS doc
    FROM {_READ_STAGED} JOIN batch_files USING (file_idx)
), fields AS (
    SELECT line, source_file, json_type(doc) = 'OBJECT' AS is_object,
           json_extract_string(doc, [
               '$.id', '$.type', '$.actor.id', '$.actor.login', '$.repo.id', '$.repo.name',
               '$.payload.action', '$.payload.ref_type', '$.created_at']) AS f
    FROM lines
), typed AS (
    SELECT line, source_file, is_object,
           try_cast(f[1] AS BIGINT) AS event_id,
           nullif(trim(f[2]), '') AS event_type,
           try_cast(f[3] AS BIGINT) AS actor_id,
           nullif(trim(f[4]), '') AS actor_login,
           try_cast(f[5] AS BIGINT) AS repo_id,
           nullif(trim(f[6]), '') AS repo_name,
           f[7] AS action,
           f[8] AS ref_type,
           (try_cast(f[9] AS TIMESTAMPTZ) AT TIME ZONE 'UTC') AS created_at
    FROM fields
)
SELECT *,
       CASE
           WHEN NOT is_object THEN 'malformed_json'
           WHEN event_id IS NULL OR event_id <= 0 THEN 'missing_event_id'
           WHEN actor_id IS NULL OR actor_id <= 0 THEN 'missing_actor'
           WHEN event_type IS NULL THEN 'missing_type'
           WHEN created_at IS NULL THEN 'unparseable_timestamp'
           -- GitHub launched in 2008; nothing can have happened after the moment we read it.
           WHEN created_at < TIMESTAMP '2008-01-01' OR created_at > ?::TIMESTAMP + INTERVAL 1 DAY
               THEN 'timestamp_out_of_range'
       END AS reject_reason
FROM typed
"""


def _connect(settings: Settings) -> duckdb.DuckDBPyConnection:
    tmp = settings.data_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(
        config={"memory_limit": settings.duckdb_memory, "threads": settings.duckdb_threads, "temp_directory": str(tmp)}
    )
    con.execute("SET TimeZone = 'UTC'")
    con.execute("SET preserve_insertion_order = false")
    return con


def _seen(con: duckdb.DuckDBPyConnection, manifest_dir: Path) -> tuple[set[tuple[str, int]], int]:
    if not any(manifest_dir.glob("*.parquet")):
        return set(), 0
    glob = str(manifest_dir / "*.parquet")
    rows = con.execute("SELECT source_file, size_bytes FROM read_parquet(?)", [glob]).fetchall()
    last = con.execute("SELECT max(batch_id) FROM read_parquet(?)", [glob]).fetchone()
    return {(r[0], r[1]) for r in rows}, int(last[0]) if last and last[0] is not None else 0


def _batches(files: list[tuple[Path, int]]) -> list[list[tuple[Path, int]]]:
    out: list[list[tuple[Path, int]]] = [[]]
    size = 0
    for f, nbytes in files:
        if out[-1] and (len(out[-1]) >= BATCH_MAX_FILES or size + nbytes > BATCH_MAX_BYTES):
            out.append([])
            size = 0
        out[-1].append((f, nbytes))
        size += nbytes
    return [b for b in out if b]


def _stage_lines(files: list[tuple[Path, int]], staged: Path) -> list[tuple[int, str, int, str | None]]:
    """Decompress a batch into one text file, each line prefixed with its file's index.

    Thousands of small hourly files cost DuckDB several milliseconds each to open; one staged file does
    not. This is also where an unreadable file (truncated download, not gzip) is caught: nothing of it
    is staged and it is recorded as rejected. Returns (file_idx, name, size, error) per file.
    """
    result: list[tuple[int, str, int, str | None]] = []
    with open(staged, "wb") as out:
        for idx, (f, nbytes) in enumerate(files):
            start = out.tell()
            prefix = str(idx).encode() + SEP
            try:
                with gzip.open(f, "rb") as src:
                    at_line_start = True
                    while chunk := src.read(8 << 20):
                        # A raw 0x01 is illegal inside JSON, so replacing it cannot turn a bad line into a good one.
                        chunk = chunk.replace(SEP, b"?")
                        if at_line_start:
                            out.write(prefix)
                        at_line_start = chunk.endswith(b"\n")
                        out.write((chunk[:-1] if at_line_start else chunk).replace(b"\n", b"\n" + prefix))
                        if at_line_start:
                            out.write(b"\n")
                    if not at_line_start:
                        out.write(b"\n")
                result.append((idx, f.name, nbytes, None))
            except (OSError, EOFError, zlib.error) as exc:
                out.seek(start)
                out.truncate()
                result.append((idx, f.name, nbytes, f"{type(exc).__name__}: {exc}"[:300]))
                warn(logger, "file rejected", file=f.name, error=str(exc)[:300])
    return result


def _load_batch(
    con: duckdb.DuckDBPyConnection, settings: Settings, batch_id: int, files: list[tuple[Path, int]], now: str
) -> dict[str, int]:
    """Parse, validate and write one batch."""
    staging = settings.data_dir / "tmp" / f"ingest_{batch_id:06d}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    staged = _stage_lines(files, staging / "lines.txt")
    # Passed as one JSON parameter: binding thousands of Python values one by one is slow in DuckDB.
    con.execute(
        """CREATE OR REPLACE TEMP TABLE batch_files AS
           SELECT unnest(from_json(?::JSON,
               '[{"file_idx": "INTEGER", "source_file": "VARCHAR", "size_bytes": "BIGINT", "error": "VARCHAR"}]'
           ), recursive := true)""",
        [json.dumps([dict(zip(("file_idx", "source_file", "size_bytes", "error"), r, strict=True)) for r in staged])],
    )
    con.execute(_PARSE_SQL, [str(staging / "lines.txt"), now])

    # batch_id and now are produced by this module, never by a caller's input.
    stamp = f"{batch_id}::INTEGER AS batch_id, '{now}'::TIMESTAMP AS ingested_at"
    # Sorted by time so row-group min/max statistics prune well when a later run asks for specific days.
    con.execute(
        f"""COPY (
            SELECT event_id, event_type, actor_id, actor_login, repo_id, repo_name, action, ref_type, created_at,
                   CAST(created_at AS DATE) AS event_date, source_file, {stamp}
            FROM parsed WHERE reject_reason IS NULL ORDER BY created_at
        ) TO '{(staging / "events.parquet").as_posix()}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)"""
    )
    con.execute(
        f"""COPY (
            SELECT source_file, reject_reason, left(line, 4000) AS raw_line, length(line) AS raw_length, {stamp}
            FROM parsed WHERE reject_reason IS NOT NULL
        ) TO '{(staging / "quarantine.parquet").as_posix()}' (FORMAT parquet, COMPRESSION zstd)"""
    )
    con.execute(
        f"""COPY (
            SELECT b.source_file, b.size_bytes,
                   CASE WHEN b.error IS NULL THEN 'ingested' ELSE 'rejected' END AS status, b.error,
                   count(p.source_file) AS lines_read,
                   count(p.source_file) FILTER (WHERE p.reject_reason IS NULL) AS lines_accepted,
                   count(p.source_file) FILTER (WHERE p.reject_reason IS NOT NULL) AS lines_quarantined, {stamp}
            FROM batch_files b LEFT JOIN parsed p USING (source_file)
            GROUP BY ALL
        ) TO '{(staging / "manifest.parquet").as_posix()}' (FORMAT parquet)"""
    )
    stats = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE reject_reason IS NULL), "
        "count(*) FILTER (WHERE reject_reason IS NOT NULL) FROM parsed"
    ).fetchone()
    assert stats is not None

    name = f"batch_{batch_id:06d}.parquet"
    for part, target_dir in (
        ("events", settings.bronze_dir),
        ("quarantine", settings.quarantine_dir),
        ("manifest", settings.data_dir / "manifest"),  # commit marker: last
    ):
        target_dir.mkdir(parents=True, exist_ok=True)
        (staging / f"{part}.parquet").replace(target_dir / name)
    shutil.rmtree(staging, ignore_errors=True)
    rejected = sum(1 for r in staged if r[3])
    return {
        "files": len(staged) - rejected,
        "files_rejected": rejected,
        "lines": stats[0],
        "accepted": stats[1],
        "quarantined": stats[2],
    }


def ingest(settings: Settings) -> dict[str, int]:
    """Ingest every raw file not yet in the manifest. Safe to re-run; a second run with nothing new is a no-op."""
    started = time.perf_counter()
    con = _connect(settings)
    try:
        seen, last_batch = _seen(con, settings.data_dir / "manifest")
        sized = ((f, f.stat().st_size) for f in sorted(settings.raw_dir.glob("*.json.gz")))
        todo = [(f, nbytes) for f, nbytes in sized if (f.name, nbytes) not in seen]
        totals = {"files": 0, "files_rejected": 0, "lines": 0, "accepted": 0, "quarantined": 0, "batches": 0}
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        for i, batch in enumerate(_batches(todo), start=1):
            stats = _load_batch(con, settings, last_batch + i, batch, now)
            for k, v in stats.items():
                totals[k] += v
            totals["batches"] += 1
            log(logger, "batch ingested", batch_id=last_batch + i, **stats)
    finally:
        con.close()
    log(logger, "ingest finished", seconds=round(time.perf_counter() - started, 2), **totals)
    return totals


def fetch(
    settings: Settings, hours: list[str], attempts: int = 5, base_url: str = "https://data.gharchive.org"
) -> list[Path]:
    """Download GH Archive hours (`2025-03-12-15`; the hour is not zero-padded upstream) into the raw directory.

    Retries with exponential backoff, capped. A file only gets its final name once its length matches
    what the server announced, so an interrupted download can never be ingested as if it were complete.
    """
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    done: list[Path] = []
    for hour in hours:
        date, _, h = hour.rpartition("-")
        name = f"{date}-{int(h)}.json.gz"
        target = settings.raw_dir / name
        if target.exists():
            done.append(target)
            continue
        part = target.with_suffix(".part")
        for attempt in range(1, attempts + 1):
            try:
                with urllib.request.urlopen(f"{base_url}/{name}", timeout=60) as resp, open(part, "wb") as out:  # noqa: S310
                    expected = int(resp.headers.get("Content-Length", "-1"))
                    shutil.copyfileobj(resp, out, length=1 << 20)
                if expected >= 0 and part.stat().st_size != expected:
                    raise OSError(f"short read: {part.stat().st_size} of {expected} bytes")
                with open(part, "rb") as check:
                    if check.read(2) != b"\x1f\x8b":
                        raise OSError("not a gzip file")
                part.replace(target)
                done.append(target)
                log(logger, "fetched", file=name, bytes=target.stat().st_size, attempt=attempt)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # the archive has gaps; a missing hour is a fact, not a failure
                    part.unlink(missing_ok=True)
                    warn(logger, "hour not in archive", file=name)
                    break
                err: Exception = exc
            except OSError as exc:
                err = exc
            part.unlink(missing_ok=True)
            if attempt == attempts:
                raise RuntimeError(f"could not fetch {name} after {attempts} attempts: {err}")
            delay = min(60.0, 2.0**attempt)
            warn(logger, "fetch retry", file=name, attempt=attempt, wait_seconds=delay, error=str(err))
            time.sleep(delay)
    return done
