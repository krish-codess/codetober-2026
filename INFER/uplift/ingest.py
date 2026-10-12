"""Raw experiment files -> validated units in PostgreSQL, with everything that fails kept in quarantine.

One contract for every source: the Hillstrom columns, plus optional `customer_id`, `assigned_at`, `true_uplift`.
The real file and the synthetic one go through exactly the same code.

  * A missing FEATURE is data (NULL); the unit is loaded.
  * A missing or impossible treatment/outcome, or a feature value outside its domain, quarantines the row.
  * Known format variants ("$1,234.50", " web ", "Y", 01/31/2026 10:00:00) are normalised, not rejected.
  * The raw file is copied, content-addressed, before anything is parsed. Ingesting the same bytes twice is a no-op.
  * A file is loaded in one transaction: a crash leaves nothing behind.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import shutil
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uplift import db
from uplift.config import HILLSTROM_SHA256, HILLSTROM_URL, Settings, log, warn

logger = logging.getLogger("uplift.ingest")

OUTCOMES = ("visit", "conversion", "spend")
REQUIRED = ["recency", "history", "mens", "womens", "zip_code", "newbie", "channel", "segment", *OUTCOMES]
ZIP_CODES = {"surburban": "Surburban", "urban": "Urban", "rural": "Rural"}  # "Surburban" is how the source spells it
CHANNELS = {"web": "Web", "phone": "Phone", "multichannel": "Multichannel"}
TREATMENT = {"mens e-mail": True, "womens e-mail": True, "no e-mail": False}
_TRUE, _FALSE = {"1", "y", "yes", "true", "t"}, {"0", "n", "no", "false", "f"}
UNIT_COLUMNS = [
    "dataset_id",
    "unit_id",
    "raw_sha",
    "line_no",
    "treatment",
    "outcome",
    "assigned_at",
    "recency",
    "history",
    "mens",
    "womens",
    "newbie",
    "zip_code",
    "channel",
    "true_uplift",
]


class Reject(Exception):
    """A row that must not be loaded. The message is the quarantine reason."""


def _bool(row: dict[str, str], col: str) -> bool | None:
    v = row[col].strip().lower()
    if v == "":
        return None
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise Reject(f"invalid_{col}")


def _number(row: dict[str, str], col: str) -> float | None:
    v = row[col].strip().replace("$", "").replace(",", "")
    if v == "":
        return None
    try:
        x = float(v)
    except ValueError:
        raise Reject(f"invalid_{col}") from None
    if not math.isfinite(x) or x < 0:
        raise Reject(f"invalid_{col}")
    return x


def _category(row: dict[str, str], col: str, known: dict[str, str]) -> str | None:
    v = row[col].strip().lower()
    if v == "":
        return None
    if v not in known:
        raise Reject(f"invalid_{col}")
    return known[v]


def _timestamp(value: str) -> datetime | None:
    v = value.strip()
    if v == "":
        return None
    try:
        ts = datetime.fromisoformat(v)
    except ValueError:
        try:
            ts = datetime.strptime(v, "%m/%d/%Y %H:%M:%S")  # noqa: DTZ007 - made aware on the next line
        except ValueError:
            raise Reject("invalid_assigned_at") from None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def normalize(row: dict[str, str], outcome: str) -> dict[str, object]:
    """One raw row -> one clean unit, or Reject(reason). Pure: this is the whole validation contract."""
    if None in row or any(v is None for v in row.values()):
        raise Reject("malformed_row")  # more or fewer cells than the header
    treatment = TREATMENT.get(row["segment"].strip().lower())
    if treatment is None:
        raise Reject("invalid_segment")
    y = _number(row, outcome)
    if y is None:
        raise Reject("missing_outcome")
    if outcome != "spend" and y not in (0.0, 1.0):
        raise Reject(f"invalid_{outcome}")
    recency = _number(row, "recency")
    if recency is not None and not (recency.is_integer() and 1 <= recency <= 12):
        raise Reject("invalid_recency")
    true_uplift = row.get("true_uplift", "").strip()
    return {
        "treatment": treatment,
        "outcome": y,
        "assigned_at": _timestamp(row.get("assigned_at", "")),
        "recency": None if recency is None else int(recency),
        "history": _number(row, "history"),
        "mens": _bool(row, "mens"),
        "womens": _bool(row, "womens"),
        "newbie": _bool(row, "newbie"),
        "zip_code": _category(row, "zip_code", ZIP_CODES),
        "channel": _category(row, "channel", CHANNELS),
        "true_uplift": float(true_uplift) if true_uplift else None,
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


Quarantined = tuple[int, str | None, str, str]  # line number, unit id, reason, the raw row as JSON
_VALUES = UNIT_COLUMNS[4:]


def _raw_rows(path: Path, wanted: set[int]) -> dict[int, str]:
    """The raw rows at these line numbers, re-read from the file. Clean rows are not kept in memory as text."""
    if not wanted:
        return {}
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        return {n: json.dumps(row, default=str) for n, row in enumerate(csv.DictReader(fh), start=2) if n in wanted}


def parse(path: Path, sha: str, outcome: str) -> tuple[list[tuple[Any, ...]], list[Quarantined]]:
    """Validate a file into (units, quarantined rows). Each raw line ends up in exactly one of the two.

    A unit is the tuple (unit_id, line_no, *values in UNIT_COLUMNS order).
    """
    good: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    bad: list[Quarantined] = []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{path.name}: required columns missing from the header: {', '.join(missing)}")
        has_id = "customer_id" in (reader.fieldnames or [])
        for line_no, row in enumerate(reader, start=2):
            # Without a key in the file (Hillstrom has none) a row is its own unit, and duplicates cannot be told
            # from two customers who look alike. See docs/data-profile.md.
            unit_id = (row.get("customer_id") or "").strip() if has_id else f"{sha[:8]}-{line_no}"
            try:
                if not unit_id:
                    raise Reject("missing_customer_id")
                unit = normalize(row, outcome)
            except Reject as rejected:
                bad.append((line_no, unit_id or None, str(rejected), json.dumps(row, default=str)))
                continue
            good[unit_id].append((unit_id, line_no, *[unit[c] for c in _VALUES]))

    units: list[tuple[Any, ...]] = []
    repeated: list[tuple[int, str, str]] = []
    for unit_id, copies in good.items():
        if len(copies) == 1:
            units.append(copies[0])
        elif all(c[2:] == copies[0][2:] for c in copies):  # resends of an identical row: keep one
            units.append(copies[0])
            repeated += [(c[1], unit_id, "duplicate_row") for c in copies[1:]]
        else:  # the same unit recorded two different ways (e.g. in both arms): none of the copies can be trusted
            repeated += [(c[1], unit_id, "conflicting_duplicate") for c in copies]
    raw = _raw_rows(path, {line_no for line_no, _, _ in repeated})
    bad += [(line_no, unit_id, reason, raw[line_no]) for line_no, unit_id, reason in repeated]
    return units, bad


def ingest(settings: Settings, path: Path, dataset: str, outcome: str = "visit") -> dict[str, object]:
    if outcome not in OUTCOMES:
        raise SystemExit(f"outcome must be one of {OUTCOMES}")
    started = time.perf_counter()
    sha = sha256_file(path)
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    preserved = settings.raw_dir / f"{sha}.csv"
    if not preserved.exists():  # immutable, content-addressed copy; derived tables point back at it by hash
        shutil.copyfile(path, preserved)
        preserved.chmod(0o444)

    with db.connect(settings.batch_database_url) as conn:
        if conn.execute("SELECT 1 FROM raw_file WHERE sha256 = %s", [sha]).fetchone():
            log(logger, "file already ingested, nothing to do", file=path.name, sha256=sha)
            return {"file": path.name, "sha256": sha, "skipped": True}
        conn.execute(
            "INSERT INTO dataset (name, outcome) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING", [dataset, outcome]
        )
        ds = conn.execute("SELECT id, outcome FROM dataset WHERE name = %s", [dataset]).fetchone()
        assert ds is not None
        if ds["outcome"] != outcome:
            raise SystemExit(
                f"dataset {dataset!r} already uses outcome {ds['outcome']!r}; refusing to mix in {outcome!r}"
            )

        units, bad = parse(preserved, sha, outcome)
        total = len(units) + len(bad)
        conn.execute(
            "INSERT INTO raw_file (sha256, dataset_id, filename, bytes, rows_total, rows_loaded, rows_quarantined)"
            " VALUES (%s, %s, %s, %s, %s, 0, %s)",
            [sha, ds["id"], path.name, preserved.stat().st_size, total, total],
        )
        conn.execute("CREATE TEMP TABLE staging (LIKE unit INCLUDING DEFAULTS) ON COMMIT DROP")
        with conn.cursor().copy(f"COPY staging ({', '.join(UNIT_COLUMNS)}) FROM STDIN") as copy:  # noqa: S608 - constant column list
            for unit_id, line_no, *values in units:
                copy.write_row([ds["id"], unit_id, sha, line_no, *values])
        loaded = conn.execute(
            "INSERT INTO unit SELECT * FROM staging ON CONFLICT (dataset_id, unit_id) DO NOTHING"
        ).rowcount
        # A unit that an earlier file already delivered: keep the first, record the repeat.
        repeats = conn.execute(
            "SELECT s.line_no, s.unit_id FROM staging s JOIN unit u USING (dataset_id, unit_id) WHERE u.raw_sha <> s.raw_sha"
        ).fetchall()
        raw = _raw_rows(preserved, {r["line_no"] for r in repeats})
        bad += [(r["line_no"], r["unit_id"], "unit_already_loaded", raw[r["line_no"]]) for r in repeats]
        with conn.cursor().copy("COPY quarantine (raw_sha, line_no, unit_id, reason, raw) FROM STDIN") as copy:
            for row in bad:
                copy.write_row([sha, *row])
        conn.execute(
            "UPDATE raw_file SET rows_loaded = %s, rows_quarantined = %s WHERE sha256 = %s", [loaded, len(bad), sha]
        )

    seconds = time.perf_counter() - started
    result: dict[str, object] = {
        "file": path.name,
        "sha256": sha,
        "rows_total": total,
        "rows_loaded": loaded,
        "rows_quarantined": len(bad),
        "quarantine_reasons": dict(Counter(reason for _, _, reason, _ in bad)),
        "rows_per_second": round(total / seconds),
    }
    log(logger, "file ingested", dataset=dataset, **result)
    return result


def fetch_hillstrom(settings: Settings, delays: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)) -> Path:
    """Download the Hillstrom file once. Retries with capped backoff; refuses a file with the wrong hash."""
    dest = settings.data_dir / "download" / "hillstrom.csv"
    pinned = settings.hillstrom_url == HILLSTROM_URL
    if dest.exists() and (not pinned or sha256_file(dest) == HILLSTROM_SHA256):
        return dest
    if not settings.hillstrom_url.startswith(("http://", "https://")):
        raise SystemExit("UPLIFT_HILLSTROM_URL must be an http(s) URL")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    for attempt, delay in enumerate((*delays, None), start=1):
        try:
            with urllib.request.urlopen(settings.hillstrom_url, timeout=30) as resp, tmp.open("wb") as fh:  # noqa: S310 - scheme checked above
                shutil.copyfileobj(resp, fh)
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if delay is None:
                raise SystemExit(
                    f"could not download the Hillstrom file after {attempt} attempts ({exc}). The synthetic dataset"
                    " needs no network: run `uplift seed --no-hillstrom`, or place the file at "
                    f"{dest} yourself."
                ) from exc
            warn(logger, "download failed, retrying", attempt=attempt, retry_in_seconds=delay, error=str(exc))
            time.sleep(delay)
    if pinned and sha256_file(tmp) != HILLSTROM_SHA256:
        tmp.unlink()
        raise SystemExit("downloaded Hillstrom file does not match the pinned SHA-256; refusing to use it")
    tmp.replace(dest)
    return dest
