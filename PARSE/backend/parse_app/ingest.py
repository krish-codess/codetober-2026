"""Boundary validation and ingestion of a JSONL feed file.

`validate` is a pure function (bytes -> Accepted | Rejected). `ingest_file` is one transaction:
either the whole file lands or none of it, and a file is ingested at most once (keyed by its
SHA-256), so the stage is safe to retry. Every raw line is stored byte-for-byte and ends up in
exactly one of `feedback` or `quarantine`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from .db import bulk_insert
from .embed import normalize_text
from .fetch import sha256_file
from .log import event, metrics
from .taxonomy import active_paths, canonical_path, current_version, resolve, slug

logger = logging.getLogger(__name__)

MAX_CHARS = 4000
MAX_LINE_BYTES = 64_000
LATE_AFTER = timedelta(hours=48)  # event time this far behind the source's watermark = late
FUTURE_SLACK = timedelta(days=1)  # tolerated clock skew
_SOURCE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_TS_FORMATS = ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


@dataclass(frozen=True)
class Accepted:
    source: str
    external_id: str
    text: str
    text_sha256: str
    lang: str | None
    created_at: datetime | None
    group_key: str
    split: str
    gold: tuple[str, tuple[str, ...]] | None  # (domain, raw categories)
    repaired: bool


@dataclass(frozen=True)
class Rejected:
    reason: str
    detail: str


def _clean(text_: str) -> tuple[str, bool]:
    """Repair double-decoded UTF-8, drop control characters, normalise. Returns (text, repaired)."""
    repaired = False
    try:
        # Mojibake test: text that fits in Latin-1 AND whose Latin-1 bytes are valid multi-byte
        # UTF-8 was UTF-8 decoded with the wrong codec. Genuine Latin-1 text ("café") fails the
        # second step, so this does not touch it.
        fixed = text_.encode("latin-1").decode("utf-8")
        if fixed != text_:
            text_, repaired = fixed, True
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    text_ = "".join(ch if ch in "\n\t" or unicodedata.category(ch)[0] != "C" else " " for ch in text_)
    return normalize_text(text_), repaired


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not 0 < value < 4_102_444_800:  # 1970..2100 in seconds
            return None
        return datetime.fromtimestamp(value, tz=UTC)
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        for fmt in _TS_FORMATS:
            try:
                dt = datetime.strptime(value.strip(), fmt)  # noqa: DTZ007 - tz assigned below
                break
            except ValueError:
                continue
        else:
            return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def validate(raw: bytes, default_source: str, now: datetime) -> Accepted | Rejected:
    if len(raw) > MAX_LINE_BYTES:
        return Rejected("too_long", f"line is {len(raw)} bytes (limit {MAX_LINE_BYTES})")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return Rejected("bad_encoding", f"invalid UTF-8 at byte {e.start}")
    try:
        obj = json.loads(decoded)
    except json.JSONDecodeError as e:
        return Rejected("malformed_json", f"{e.msg} at column {e.colno}")
    if not isinstance(obj, dict):
        return Rejected("malformed_json", f"expected an object, got {type(obj).__name__}")

    if "text" not in obj or obj["text"] is None:
        return Rejected("missing_text", 'field "text" is absent')
    if not isinstance(obj["text"], str):
        return Rejected("bad_field", f'"text" must be a string, got {type(obj["text"]).__name__}')
    text_, repaired = _clean(obj["text"])
    if not text_:
        return Rejected("empty_text", '"text" is empty after whitespace/control-character removal')
    if len(text_) > MAX_CHARS:
        return Rejected("too_long", f"text is {len(text_)} characters (limit {MAX_CHARS})")

    source = obj.get("source", default_source)
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        return Rejected("bad_field", '"source" must match [a-z0-9][a-z0-9_-]{0,31}')

    # Language tag: accept "EN", "en-US", "sw_KE"; anything else is treated as "not supplied".
    lang = obj.get("lang")
    lang = re.split(r"[-_]", lang.strip().lower())[0] if isinstance(lang, str) else None
    if lang is not None and not re.fullmatch(r"[a-z]{2,3}", lang):
        lang = None

    created_at = None
    if obj.get("created_at") is not None:
        created_at = _parse_ts(obj["created_at"])
        if created_at is None:
            return Rejected("bad_timestamp", f"unparseable created_at {str(obj['created_at'])[:40]!r}")
        if created_at > now + FUTURE_SLACK:
            return Rejected("bad_timestamp", f"created_at {created_at.isoformat()} is in the future")

    text_sha = hashlib.sha256(text_.encode()).hexdigest()
    external_id = obj.get("id")
    if external_id is None:  # content-derived, so a redelivery of the same record still dedupes
        external_id = "sha:" + text_sha[:32]
    if not isinstance(external_id, str) or not 1 <= len(external_id) <= 200:
        return Rejected("bad_field", '"id" must be a string of 1-200 characters')

    is_eval = obj.get("eval", False)
    if not isinstance(is_eval, bool):
        return Rejected("bad_field", '"eval" must be a boolean')
    gold = None
    if obj.get("gold") is not None:
        g = obj["gold"]
        cats = g.get("categories") if isinstance(g, dict) else None
        if (
            not isinstance(g, dict)
            or not isinstance(g.get("domain"), str)
            or not isinstance(cats, list)
            or not all(isinstance(c, str) for c in cats)
        ):
            return Rejected("bad_field", '"gold" must be {"domain": str, "categories": [str]}')
        gold = (g["domain"], tuple(cats))
    if is_eval and gold is None:
        return Rejected("bad_field", "eval records must carry gold labels")

    group = obj.get("group")
    if group is not None and not (isinstance(group, str) and 1 <= len(group) <= 200):
        return Rejected("bad_field", '"group" must be a string of 1-200 characters')

    return Accepted(
        source=source,
        external_id=external_id,
        text=text_,
        text_sha256=text_sha,
        lang=lang,
        created_at=created_at,
        group_key=group or f"id:{source}:{external_id}",
        split="test" if is_eval else "pool",
        gold=gold,
        repaired=repaired,
    )


def gold_paths(gold: tuple[str, tuple[str, ...]], known: dict[str, int]) -> tuple[set[int], int]:
    """Gold (domain, raw categories) -> most-specific known node ids, and how many were unmappable."""
    domain, cats = gold
    nodes: set[int] = set()
    unmapped = 0
    if slug(domain) in known:
        nodes.add(known[slug(domain)])  # a sentence with no aspect still belongs to its domain
    for cat in cats:
        path = canonical_path(domain, cat)
        hit = resolve(path, known) if path else None
        if hit is None:
            unmapped += 1
        else:
            nodes.add(known[hit])
    return nodes, unmapped


def ingest_file(
    engine: Engine, path: Path, default_source: str = "feed", now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    file_sha = sha256_file(path)
    with engine.begin() as conn:
        batch_id = conn.execute(
            text(
                """INSERT INTO ingest_batches (source, file_name, file_sha256) VALUES (:s, :f, :h)
                   ON CONFLICT (file_sha256) DO NOTHING RETURNING id"""
            ),
            {"s": default_source, "f": path.name, "h": file_sha},
        ).scalar()
        if batch_id is None:
            row = conn.execute(
                text(
                    """SELECT id AS batch_id, n_records, n_accepted, n_quarantined, n_repaired, n_late
                       FROM ingest_batches WHERE file_sha256 = :h"""
                ),
                {"h": file_sha},
            ).one()
            event(logger, "ingest_skipped_already_done", batch_id=row.batch_id, file=path.name)
            return {**row._asdict(), "already_ingested": True}

        known = active_paths(conn)
        version = current_version(conn)
        # ponytail: whole-table dedupe maps in memory (fine to ~1M rows). Beyond that, COPY the
        # batch into a temp table and resolve duplicates with a join against feedback.
        seen = {
            (r.source, r.external_id): r.text_sha256
            for r in conn.execute(text("SELECT source, external_id, text_sha256 FROM feedback"))
        }
        groups = {r.group_key: r.split for r in conn.execute(text("SELECT group_key, split FROM split_groups"))}
        watermark = {
            r.source: r.wm
            for r in conn.execute(text("SELECT source, max(created_at) AS wm FROM feedback GROUP BY source"))
            if r.wm is not None
        }

        raw_rows: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        accepted: list[dict[str, Any]] = []
        gold_by_line: dict[int, tuple[str, tuple[str, ...]]] = {}
        new_groups: list[dict[str, str]] = []
        n_repaired = n_late = 0

        with path.open("rb") as f:
            for line_no, line in enumerate(f, start=1):
                raw = line.rstrip(b"\r\n")
                if not raw.strip():
                    continue
                raw_rows.append({"batch_id": batch_id, "line_no": line_no, "payload": raw})
                result = validate(raw, default_source, now)
                if isinstance(result, Accepted):
                    key = (result.source, result.external_id)
                    if key in seen:
                        same = seen[key] == result.text_sha256
                        result = Rejected(
                            "duplicate_delivery" if same else "conflicting_duplicate",
                            f"id {result.external_id!r} already ingested"
                            + ("" if same else " with different text; first version kept"),
                        )
                    elif groups.get(result.group_key, result.split) != result.split:
                        result = Rejected(
                            "split_conflict",
                            f"group {result.group_key!r} is already in split {groups[result.group_key]!r}",
                        )
                if isinstance(result, Rejected):
                    quarantined.append(
                        {"batch_id": batch_id, "line_no": line_no, "reason": result.reason, "detail": result.detail}
                    )
                    continue
                seen[(result.source, result.external_id)] = result.text_sha256
                if result.group_key not in groups:
                    groups[result.group_key] = result.split
                    new_groups.append({"group_key": result.group_key, "split": result.split})
                late = False
                if result.created_at is not None:
                    wm = watermark.get(result.source)
                    late = wm is not None and result.created_at < wm - LATE_AFTER
                    if wm is None or result.created_at > wm:
                        watermark[result.source] = result.created_at
                n_late += late
                n_repaired += result.repaired
                if result.gold is not None and result.split == "test":
                    gold_by_line[line_no] = result.gold
                accepted.append(
                    {
                        "batch_id": batch_id,
                        "line_no": line_no,
                        "source": result.source,
                        "external_id": result.external_id,
                        "text": result.text,
                        "text_sha256": result.text_sha256,
                        "lang": result.lang,
                        "created_at": result.created_at,
                        "is_late": late,
                        "group_key": result.group_key,
                        "split": result.split,
                    }  # fmt: skip
                )

        bulk_insert(conn, "raw_feedback", {"batch_id": "int", "line_no": "int", "payload": "bytea"}, raw_rows)
        bulk_insert(
            conn, "quarantine", {"batch_id": "int", "line_no": "int", "reason": "text", "detail": "text"}, quarantined
        )
        bulk_insert(conn, "split_groups", {"group_key": "text", "split": "text"}, new_groups)
        bulk_insert(
            conn, "feedback",
            {"batch_id": "int", "line_no": "int", "source": "text", "external_id": "text", "text": "text",
             "text_sha256": "text", "lang": "text", "created_at": "timestamptz", "is_late": "boolean",
             "group_key": "text", "split": "text"},
            accepted,
        )  # fmt: skip

        # Reference labels for the evaluation split. Pool gold stays in the raw payload, where
        # only the simulated annotator reads it.
        gold_unmapped = 0
        if gold_by_line:
            ids = dict(
                conn.execute(text("SELECT line_no, id FROM feedback WHERE batch_id = :b"), {"b": batch_id})
                .tuples()
                .all()
            )
            ann: list[dict[str, Any]] = []
            lab: list[dict[str, Any]] = []
            for line_no, gold in gold_by_line.items():
                nodes, unmapped = gold_paths(gold, known)
                gold_unmapped += unmapped
                ann.append(
                    {"feedback_id": ids[line_no], "annotator": "gold", "source": "gold", "taxonomy_version": version}
                )
                lab.extend({"feedback_id": ids[line_no], "node_id": n} for n in nodes)
            bulk_insert(
                conn, "annotations",
                {"feedback_id": "bigint", "annotator": "text", "source": "text", "taxonomy_version": "int"}, ann,
            )  # fmt: skip
            bulk_insert(conn, "labels", {"feedback_id": "bigint", "node_id": "int"}, lab, "ON CONFLICT DO NOTHING")

        # Same text submitted under a new id (double submit): keep the row, point it at the
        # first occurrence, and keep it out of the labelling queue.
        n_dupes = conn.execute(
            text(
                """UPDATE feedback f SET duplicate_of = first.id
                   FROM (SELECT text_sha256, split, min(id) AS id FROM feedback
                         WHERE split = 'pool' GROUP BY text_sha256, split) first
                   WHERE f.batch_id = :b AND f.split = 'pool' AND f.text_sha256 = first.text_sha256
                     AND f.id <> first.id AND f.duplicate_of IS NULL"""
            ),
            {"b": batch_id},
        ).rowcount

        # Leakage guard: a pool row whose text also exists in the evaluation split must never be
        # trained on (or waste an annotator's time). Same mechanism, pointing at the test row.
        n_leaks = conn.execute(
            text(
                """UPDATE feedback f SET duplicate_of = t.id
                   FROM (SELECT text_sha256, min(id) AS id FROM feedback WHERE split = 'test' GROUP BY text_sha256) t
                   WHERE f.split = 'pool' AND f.text_sha256 = t.text_sha256 AND f.duplicate_of IS NULL"""
            )
        ).rowcount

        stats = {
            "n_records": len(raw_rows), "n_accepted": len(accepted), "n_quarantined": len(quarantined),
            "n_repaired": n_repaired, "n_late": n_late,
        }  # fmt: skip
        conn.execute(
            text(
                """UPDATE ingest_batches SET n_records = :n_records, n_accepted = :n_accepted,
                   n_quarantined = :n_quarantined, n_repaired = :n_repaired, n_late = :n_late WHERE id = :id"""
            ),
            {**stats, "id": batch_id},
        )

    by_reason: dict[str, int] = {}
    for q in quarantined:
        by_reason[q["reason"]] = by_reason.get(q["reason"], 0) + 1
        metrics.inc("ingest_quarantined_total", reason=q["reason"])
    metrics.inc("ingest_accepted_total", len(accepted))
    out = {
        "batch_id": batch_id, **stats, "n_text_duplicates": n_dupes, "n_test_overlap": n_leaks,
        "gold_unmapped": gold_unmapped,
        "quarantined_by_reason": by_reason, "already_ingested": False,
    }  # fmt: skip
    event(logger, "ingest_done", file=path.name, **out)
    return out
