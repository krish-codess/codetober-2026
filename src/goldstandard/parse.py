"""Boundary parsing + validation: raw payloads -> canonical frames, with quarantine.

Nothing is dropped silently. Every raw row ends up either in the canonical output or in a
quarantine frame with the *first* rule it failed and a copy of the offending row. Fetch
failures become explicit "gap" rows, so downstream can distinguish "no listings" from
"we do not know".

The same functions consume real ESI payloads and synthetic ones: the generator writes the
exact ESI shapes (including their defects) and has no private path into this module.
"""

from __future__ import annotations

import io
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from goldstandard.raw import RawRecord

ORDER_FIELDS = (
    "order_id",
    "type_id",
    "price",
    "volume_remain",
    "volume_total",
    "is_buy_order",
    "issued",
    "duration",
    "location_id",
)
QUOTED_PRICE = re.compile(r'"price"\s*:\s*"')
HISTORY_FIELDS = ("date", "average", "highest", "lowest", "volume", "order_count")


def _schema(**cols: Any) -> pl.Schema:
    return pl.Schema(cols)


LISTING_SCHEMA = _schema(
    world_id=pl.String,
    server_id=pl.String,
    snapshot_ts=pl.Datetime("us", "UTC"),
    fetched_at=pl.Datetime("us", "UTC"),
    item_id=pl.Int64,
    order_id=pl.Int64,
    is_buy=pl.Boolean,
    price=pl.Float64,
    volume_remain=pl.Int64,
    volume_total=pl.Int64,
    issued=pl.Datetime("us", "UTC"),
    duration=pl.Int32,
    location_id=pl.Int64,
    raw_sha=pl.String,
)
QUARANTINE_SCHEMA = _schema(
    world_id=pl.String,
    server_id=pl.String,
    snapshot_ts=pl.Datetime("us", "UTC"),
    item_id=pl.Int64,
    reason=pl.String,
    raw_sha=pl.String,
    raw_row=pl.String,
)
GAP_SCHEMA = _schema(
    world_id=pl.String,
    server_id=pl.String,
    snapshot_ts=pl.Datetime("us", "UTC"),
    item_id=pl.Int64,
    status=pl.Int32,
    raw_sha=pl.String,
)
HISTORY_SCHEMA = _schema(
    server_id=pl.String,
    item_id=pl.Int64,
    day=pl.Date,
    average=pl.Float64,
    highest=pl.Float64,
    lowest=pl.Float64,
    volume=pl.Int64,
    order_count=pl.Int64,
    fetched_at=pl.Datetime("us", "UTC"),
    raw_sha=pl.String,
)


@dataclass
class Parsed:
    valid: pl.DataFrame
    quarantine: pl.DataFrame
    gaps: pl.DataFrame
    coerced: int = 0

    @property
    def reasons(self) -> dict[str, int]:
        if self.quarantine.is_empty():
            return {}
        counts = self.quarantine.group_by("reason").len().sort("reason")
        return {str(r): int(n) for r, n in counts.iter_rows()}


def _as_text(v: Any) -> str | None:
    """JSON-encode a raw value so strings stay distinguishable from numbers ("1.5" vs 1.5)."""
    return None if v is None else json.dumps(v)


def _ts(col: pl.Expr) -> pl.Expr:
    """Accept ISO-8601 with Z (ESI) and the space-separated variant seen in some exports."""
    return pl.coalesce(
        col.str.strptime(pl.Datetime("us"), "%Y-%m-%dT%H:%M:%SZ", strict=False),
        col.str.strptime(pl.Datetime("us"), "%Y-%m-%dT%H:%M:%S%.fZ", strict=False),
        col.str.strptime(pl.Datetime("us"), "%Y-%m-%d %H:%M:%S", strict=False),
    ).dt.replace_time_zone("UTC")


def _naive(ts: list[datetime]) -> np.ndarray:
    """UTC datetimes -> numpy datetime64 (numpy has no time zones; we re-attach UTC in polars)."""
    return np.array([t.replace(tzinfo=None) for t in ts], dtype="datetime64[us]")


def _first_failure(rules: list[tuple[str, pl.Expr]]) -> pl.Expr:
    expr: pl.Expr = pl.lit(None, dtype=pl.String)
    for reason, failed in reversed(rules):
        expr = pl.when(failed).then(pl.lit(reason)).otherwise(expr)
    return expr


def parse_order_bundles(
    world_id: str, records: Iterable[RawRecord], known_items: set[int], known_servers: set[str]
) -> Parsed:
    """Raw order-book bundles (one per server snapshot) -> listings, quarantine, gaps."""
    pages: list[str] = []  # verbatim JSON arrays of dict rows, parsed in one native pass below
    meta: dict[str, list[Any]] = {
        k: [] for k in ("requested_type", "server_id", "snapshot_ts", "fetched_at", "raw_sha", "n")
    }
    raw_rows: list[Any] = []
    quarantined: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    coerced = 0

    for rec in records:
        server_id = rec.meta.get("server_id")
        snap = rec.observed_at.astimezone(UTC)
        fetched = rec.fetched_at.astimezone(UTC)
        sha = rec.ref.sha256[:16]

        def q(
            reason: str,
            row: Any,
            item: int | None = None,
            *,
            _s: str | None = server_id,
            _t: datetime = snap,
            _h: str = sha,
        ) -> None:
            quarantined.append(
                {
                    "world_id": world_id,
                    "server_id": _s,
                    "snapshot_ts": _t,
                    "item_id": item,
                    "reason": reason,
                    "raw_sha": _h,
                    "raw_row": json.dumps(row, default=str)[:2000],
                }
            )

        def gap(item: int, status: int, *, _s: str | None = server_id, _t: datetime = snap, _h: str = sha) -> None:
            gaps.append(
                {
                    "world_id": world_id,
                    "server_id": _s,
                    "snapshot_ts": _t,
                    "item_id": item,
                    "status": status,
                    "raw_sha": _h,
                }
            )

        if server_id not in known_servers:
            q("unknown_server", rec.meta)
            continue
        try:
            responses = json.loads(rec.body)
            if not isinstance(responses, list):
                raise ValueError("bundle is not a list")
        except ValueError:
            q("corrupt_payload", rec.body[:500])
            continue
        for resp in responses:
            requested = resp.get("type_id") if isinstance(resp, dict) else None
            if not isinstance(requested, int):
                q("corrupt_payload", resp)
                continue
            body = resp.get("body")
            if resp.get("status") != 200 or body is None:
                gap(requested, int(resp.get("status") or 0))
                continue
            try:
                rows = json.loads(body)
                if not isinstance(rows, list):
                    raise ValueError("page is not a list")
            except ValueError:
                q("unparseable_body", str(body)[:500], requested)
                gap(requested, -1)
                continue
            if not all(isinstance(r, dict) for r in rows):
                for r in rows:
                    if not isinstance(r, dict):
                        q("bad_type", r, requested)
                rows = [r for r in rows if isinstance(r, dict)]
                body = json.dumps(rows)
            if not rows:
                continue
            coerced += len(QUOTED_PRICE.findall(body))
            pages.append(body.strip()[1:-1])
            raw_rows.extend(rows)
            for k, v in (
                ("requested_type", requested),
                ("server_id", server_id),
                ("snapshot_ts", snap),
                ("fetched_at", fetched),
                ("raw_sha", sha),
                ("n", len(rows)),
            ):
                meta[k].append(v)

    string_schema = {k: pl.String for k in ORDER_FIELDS}
    if pages:
        try:
            df = pl.read_json(io.StringIO("[" + ",".join(pages) + "]"), schema=string_schema)
        except Exception:  # a nested value somewhere: fall back to the slow, fully general path
            df = pl.DataFrame(
                {k: [_as_text(r.get(k)) for r in raw_rows] for k in ORDER_FIELDS}, schema=string_schema
            ).with_columns(pl.all().str.strip_chars('"'))
    else:
        df = pl.DataFrame(schema=string_schema)
    counts = meta.pop("n")
    df = df.with_columns(
        pl.Series("requested_type", np.repeat(meta["requested_type"], counts), dtype=pl.Int64),
        pl.Series("server_id", np.repeat(np.array(meta["server_id"], dtype=object), counts), dtype=pl.String),
        pl.Series("snapshot_ts", np.repeat(_naive(meta["snapshot_ts"]), counts)).dt.replace_time_zone("UTC"),
        pl.Series("fetched_at", np.repeat(_naive(meta["fetched_at"]), counts)).dt.replace_time_zone("UTC"),
        pl.Series("raw_sha", np.repeat(np.array(meta["raw_sha"], dtype=object), counts), dtype=pl.String),
    ).with_row_index("_row")
    num = {
        "order_id": pl.col("order_id").cast(pl.Int64, strict=False),
        "type_id": pl.col("type_id").cast(pl.Int64, strict=False),
        "price": pl.col("price").cast(pl.Float64, strict=False),
        "volume_remain": pl.col("volume_remain").cast(pl.Float64, strict=False),
        "volume_total": pl.col("volume_total").cast(pl.Float64, strict=False),
        "duration": pl.col("duration").cast(pl.Int32, strict=False),
        "location_id": pl.col("location_id").cast(pl.Int64, strict=False),
    }
    typed = df.with_columns(
        **{f"_{k}": v for k, v in num.items()},
        _is_buy=pl.col("is_buy_order")
        .str.to_lowercase()
        .replace_strict({"true": True, "false": False}, default=None, return_dtype=pl.Boolean),
        _issued=_ts(pl.col("issued")),
    )
    coerced += typed.filter(pl.col("issued").str.contains(" ")).height
    required = ("order_id", "type_id", "price", "volume_remain", "volume_total", "is_buy_order", "issued", "duration")
    rules = [
        ("missing_field", pl.any_horizontal(pl.col(k).is_null() for k in required)),
        (
            "bad_type",
            pl.any_horizontal(
                pl.col(f"_{k}").is_null()
                for k in ("order_id", "type_id", "price", "volume_remain", "volume_total", "duration", "is_buy")
            ),
        ),
        ("type_mismatch", pl.col("_type_id") != pl.col("requested_type")),
        ("unknown_item", ~pl.col("_type_id").is_in(list(known_items))),
        ("non_positive_price", ~(pl.col("_price") > 0) | pl.col("_price").is_infinite()),
        (
            "bad_volume",
            (pl.col("_volume_remain") < 0)
            | (pl.col("_volume_total") <= 0)
            | (pl.col("_volume_remain") > pl.col("_volume_total"))
            | (pl.col("_volume_remain") != pl.col("_volume_remain").floor()),
        ),
        (
            "bad_timestamp",
            pl.col("_issued").is_null() | (pl.col("_issued") > pl.col("snapshot_ts") + timedelta(hours=1)),
        ),
    ]
    typed = typed.with_columns(_reason=_first_failure(rules))
    # duplicates: same order seen twice in one snapshot -> keep the first copy, quarantine the rest
    typed = typed.with_columns(
        _reason=pl.when(
            pl.col("_reason").is_null()
            & pl.col("_row").rank("ordinal").over("server_id", "snapshot_ts", "_order_id").gt(1)
        )
        .then(pl.lit("duplicate"))
        .otherwise(pl.col("_reason"))
    )
    good = typed.filter(pl.col("_reason").is_null())
    valid = good.select(
        pl.lit(world_id).alias("world_id"),
        "server_id",
        "snapshot_ts",
        "fetched_at",
        pl.col("_type_id").alias("item_id"),
        pl.col("_order_id").alias("order_id"),
        pl.col("_is_buy").alias("is_buy"),
        pl.col("_price").alias("price"),
        pl.col("_volume_remain").cast(pl.Int64).alias("volume_remain"),
        pl.col("_volume_total").cast(pl.Int64).alias("volume_total"),
        pl.col("_issued").alias("issued"),
        pl.col("_duration").alias("duration"),
        pl.col("_location_id").alias("location_id"),
        "raw_sha",
    ).sort("server_id", "item_id", "snapshot_ts", "order_id")
    bad = typed.filter(pl.col("_reason").is_not_null()).select(
        "_row", "server_id", "snapshot_ts", "requested_type", "_reason", "raw_sha"
    )
    for r in bad.iter_rows(named=True):
        quarantined.append(
            {
                "world_id": world_id,
                "server_id": r["server_id"],
                "snapshot_ts": r["snapshot_ts"],
                "item_id": r["requested_type"],
                "reason": r["_reason"],
                "raw_sha": r["raw_sha"],
                "raw_row": json.dumps(raw_rows[r["_row"]], default=str)[:2000],
            }
        )
    return Parsed(
        valid=valid.cast(LISTING_SCHEMA),
        quarantine=pl.DataFrame(quarantined, schema=QUARANTINE_SCHEMA),
        gaps=pl.DataFrame(gaps, schema=GAP_SCHEMA),
        coerced=coerced,
    )


def parse_history(
    records: Iterable[RawRecord], known_items: set[int], known_servers: set[str], as_of: datetime | None = None
) -> Parsed:
    """ESI daily market history -> one row per (server, item, day).

    Each fetch returns ~13 months; consecutive fetches overlap and ESI occasionally revises
    recent days. Rule: for each (server, item, day) the row from the latest fetch with
    fetched_at <= as_of wins. Deterministic given (raw files, as_of).
    """
    pages: list[str] = []
    meta: dict[str, list[Any]] = {k: [] for k in ("server_id", "item_id", "fetched_at", "raw_sha", "n")}
    quarantined: list[dict[str, Any]] = []
    for rec in records:
        if as_of is not None and rec.fetched_at > as_of:
            continue
        server_id, item_id = rec.meta.get("server_id"), rec.meta.get("type_id")
        sha = rec.ref.sha256[:16]
        try:
            rows = json.loads(rec.body)
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ValueError
        except ValueError:
            rows = None
        if server_id not in known_servers or item_id not in known_items or rows is None:
            quarantined.append(
                {
                    "world_id": "eve",
                    "server_id": server_id,
                    "snapshot_ts": rec.observed_at,
                    "item_id": item_id,
                    "reason": "corrupt_payload" if rows is None else "unknown_series",
                    "raw_sha": sha,
                    "raw_row": rec.body[:500],
                }
            )
            continue
        if not rows:
            continue
        pages.append(rec.body.strip()[1:-1])
        for k, v in (
            ("server_id", server_id),
            ("item_id", item_id),
            ("fetched_at", rec.fetched_at.astimezone(UTC)),
            ("raw_sha", sha),
            ("n", len(rows)),
        ):
            meta[k].append(v)
    string_schema = {k: pl.String for k in HISTORY_FIELDS}
    df = (
        pl.read_json(io.StringIO("[" + ",".join(pages) + "]"), schema=string_schema)
        if pages
        else pl.DataFrame(schema=string_schema)
    )
    counts = meta.pop("n")
    df = df.with_columns(
        pl.Series("server_id", np.repeat(np.array(meta["server_id"], dtype=object), counts), dtype=pl.String),
        pl.Series("item_id", np.repeat(meta["item_id"], counts), dtype=pl.Int64),
        pl.Series("fetched_at", np.repeat(_naive(meta["fetched_at"]), counts)).dt.replace_time_zone("UTC"),
        pl.Series("raw_sha", np.repeat(np.array(meta["raw_sha"], dtype=object), counts), dtype=pl.String),
    )
    t = df.with_columns(
        _day=pl.col("date").str.to_date("%Y-%m-%d", strict=False),
        **{f"_{k}": pl.col(k).cast(pl.Float64, strict=False) for k in ("average", "highest", "lowest")},
        **{f"_{k}": pl.col(k).cast(pl.Int64, strict=False) for k in ("volume", "order_count")},
    )
    rules = [
        ("missing_field", pl.any_horizontal(pl.col(k).is_null() for k in HISTORY_FIELDS)),
        ("bad_type", pl.any_horizontal(pl.col(f"_{k}").is_null() for k in ("day", *HISTORY_FIELDS[1:]))),
        ("non_positive_price", (pl.col("_lowest") <= 0) | (pl.col("_average") <= 0)),
        (
            "inconsistent_range",
            (pl.col("_lowest") > pl.col("_average") * (1 + 1e-9))
            | (pl.col("_average") > pl.col("_highest") * (1 + 1e-9)),
        ),
        ("bad_volume", (pl.col("_volume") < 0) | (pl.col("_order_count") < 0)),
        ("bad_timestamp", pl.col("_day") > pl.col("fetched_at").dt.date()),
    ]
    t = t.with_columns(_reason=_first_failure(rules))
    for r in t.filter(pl.col("_reason").is_not_null()).iter_rows(named=True):
        quarantined.append(
            {
                "world_id": "eve",
                "server_id": r["server_id"],
                "snapshot_ts": r["fetched_at"],
                "item_id": r["item_id"],
                "reason": r["_reason"],
                "raw_sha": r["raw_sha"],
                "raw_row": json.dumps({k: r[k] for k in HISTORY_FIELDS}),
            }
        )
    valid = (
        t.filter(pl.col("_reason").is_null())
        .sort("fetched_at")
        .unique(["server_id", "item_id", "_day"], keep="last", maintain_order=True)
        .select(
            "server_id",
            "item_id",
            pl.col("_day").alias("day"),
            *[pl.col(f"_{k}").alias(k) for k in HISTORY_FIELDS[1:]],
            "fetched_at",
            "raw_sha",
        )
        .sort("server_id", "item_id", "day")
    )
    return Parsed(
        valid=valid.cast(HISTORY_SCHEMA),
        quarantine=pl.DataFrame(quarantined, schema=QUARANTINE_SCHEMA),
        gaps=pl.DataFrame(schema=GAP_SCHEMA),
    )


FAUCET_TYPES = {"bounty_prizes", "agent_mission_reward"}


def parse_wallet_flows(world_id: str, records: Iterable[RawRecord], known_servers: set[str]) -> Parsed:
    """Daily faucet payouts -> (server_id, day, ref_type, amount); quarantine anything else."""
    rows: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for rec in records:
        server_id = rec.meta.get("server_id")
        sha = rec.ref.sha256[:16]
        try:
            items = json.loads(rec.body)
            if not isinstance(items, list):
                raise ValueError
        except ValueError:
            items = None
        if server_id not in known_servers or items is None:
            quarantined.append(
                {
                    "world_id": world_id,
                    "server_id": server_id,
                    "snapshot_ts": rec.observed_at,
                    "item_id": None,
                    "reason": "corrupt_payload",
                    "raw_sha": sha,
                    "raw_row": rec.body[:500],
                }
            )
            continue
        for it in items:
            try:
                amount = float(it["amount"])
                day = date.fromisoformat(it["date"])
                if it["ref_type"] not in FAUCET_TYPES or not math.isfinite(amount) or amount < 0:
                    raise ValueError("bad ref_type or amount")
            except (KeyError, TypeError, ValueError) as exc:
                quarantined.append(
                    {
                        "world_id": world_id,
                        "server_id": server_id,
                        "snapshot_ts": rec.observed_at,
                        "item_id": None,
                        "reason": "bad_type" if not isinstance(exc, KeyError) else "missing_field",
                        "raw_sha": sha,
                        "raw_row": json.dumps(it, default=str),
                    }
                )
                continue
            rows.append(
                {
                    "server_id": server_id,
                    "day": day,
                    "ref_type": it["ref_type"],
                    "amount": amount,
                    "fetched_at": rec.fetched_at,
                    "raw_sha": sha,
                }
            )
    valid = pl.DataFrame(
        rows,
        schema={
            "server_id": pl.String,
            "day": pl.Date,
            "ref_type": pl.String,
            "amount": pl.Float64,
            "fetched_at": pl.Datetime("us", "UTC"),
            "raw_sha": pl.String,
        },
    )
    valid = valid.sort("fetched_at").unique(["server_id", "day", "ref_type"], keep="last").sort("server_id", "day")
    return Parsed(
        valid=valid,
        quarantine=pl.DataFrame(quarantined, schema=QUARANTINE_SCHEMA),
        gaps=pl.DataFrame(schema=GAP_SCHEMA),
    )


# ------------------------------------------------------------------------------------- patch notes
@dataclass(frozen=True)
class PatchNote:
    patch_id: str
    released_at: datetime
    version: str | None
    title: str
    notes: str
    is_major: bool


def parse_patch_rss(xml_text: str, default_hour_utc: int = 11) -> list[PatchNote]:
    """EVE patch-notes RSS -> dated patch events.

    Each RSS <item> is a release (e.g. "Patch Notes - Version 24.01") whose body contains
    "Patch Notes for YYYY-MM-DD.N" sections, one per deployment. A section becomes one event.
    Expansion items ("... Expansion Notes") become a major event at their pubDate. ESI gives
    no deployment time of day; EVE deploys during downtime (11:00 UTC), used as the default.
    """
    import html
    import re
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    if len(xml_text) > 20_000_000:
        raise ValueError("patch feed too large")
    root = ET.fromstring(xml_text)  # noqa: S314 - stdlib parser does not resolve external entities
    events: dict[str, PatchNote] = {}
    section = re.compile(r"<h2[^>]*>(?:<strong>)?\s*Patch Notes for (\d{4}-\d{2}-\d{2})\.(\d+)\s*(?:</strong>)?</h2>")
    tag = re.compile(r"<[^>]+>")
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        body = html.unescape(item.findtext("description") or "")
        pub = item.findtext("pubDate")
        version = None
        m = re.search(r"Version (\d+\.\d+)", title)
        if m:
            version = m[1]
        parts = section.split(body)
        if len(parts) > 1:
            # parts = [preamble, date, n, text, date, n, text, ...]
            for i in range(1, len(parts) - 2, 3):
                d, n, text = parts[i], parts[i + 1], parts[i + 2]
                pid = f"{d}.{n}"
                notes = re.sub(r"\s+", " ", tag.sub(" ", text)).strip()
                released = datetime.fromisoformat(d).replace(hour=default_hour_utc, tzinfo=UTC) + timedelta(
                    minutes=int(n) - 1
                )
                events[pid] = PatchNote(
                    pid,
                    released,
                    version,
                    f"Patch {pid}" + (f" (v{version})" if version else ""),
                    notes[:100000],
                    False,
                )
        elif pub and "Expansion" in title:
            released = parsedate_to_datetime(pub).astimezone(UTC)
            pid = "exp-" + re.sub(r"[^A-Za-z0-9]+", "-", title.split(":")[0]).strip("-").lower()
            notes = re.sub(r"\s+", " ", tag.sub(" ", body)).strip()
            events[pid] = PatchNote(pid[:64], released, version, title[:300], notes[:100000], True)
    return sorted(events.values(), key=lambda e: (e.released_at, e.patch_id))


def snapshot_day(ts: datetime) -> date:
    return ts.astimezone(UTC).date()
