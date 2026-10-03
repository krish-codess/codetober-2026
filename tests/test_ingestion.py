"""Boundary validation, raw-store immutability, generator determinism and fill inference accuracy."""

from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from goldstandard import parse
from goldstandard.fills import infer_fills
from goldstandard.raw import RawStore
from goldstandard.sources import synthetic
from tests.conftest import REFERENCE, small_config

T = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _bundle(store: RawStore, responses: list[dict], server: str = "syn-aurora", t: datetime = T):
    ref = store.put(
        source="synthetic",
        kind="orders",
        day=t.date(),
        key=f"{server}_{t:%H%M}",
        body=json.dumps(responses),
        fetched_at=t,
        observed_at=t,
        meta={"server_id": server},
    )
    return store.read(ref)


def _order(**kw):
    row = {
        "order_id": 1,
        "type_id": 34,
        "price": 4.0,
        "volume_remain": 10,
        "volume_total": 10,
        "is_buy_order": False,
        "issued": "2025-12-31T10:00:00Z",
        "duration": 90,
        "location_id": 60003760,
    }
    row.update(kw)
    return row


# ------------------------------------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ({"price": None}, "missing_field"),
        ({"price": "abc"}, "bad_type"),
        ({"type_id": 35}, "type_mismatch"),
        ({"price": -4.0}, "non_positive_price"),
        ({"price": 0}, "non_positive_price"),
        ({"volume_remain": 11}, "bad_volume"),
        ({"volume_remain": 2.5}, "bad_volume"),
        ({"issued": "2027-01-01T00:00:00Z"}, "bad_timestamp"),
        ({"issued": "yesterday"}, "bad_timestamp"),
    ],
)
def test_each_defect_is_quarantined_with_its_reason(tmp_path, known_items, row, reason):
    rec = _bundle(RawStore(tmp_path), [{"type_id": 34, "status": 200, "body": json.dumps([_order(**row)])}])
    p = parse.parse_order_bundles("synthetic", [rec], known_items, {"syn-aurora"})
    assert p.valid.height == 0
    assert p.quarantine["reason"].to_list() == [reason]
    assert json.loads(p.quarantine["raw_row"][0])  # the offending row is preserved, not dropped


def test_inconsistent_formats_are_coerced_not_rejected(tmp_path, known_items):
    rows = [
        _order(order_id=1, price="4.5"),
        _order(order_id=2, issued="2025-12-31 10:00:00"),
        _order(order_id=3, volume_remain=7.0),
        _order(order_id=4, extra_field="ignored"),
    ]
    rec = _bundle(RawStore(tmp_path), [{"type_id": 34, "status": 200, "body": json.dumps(rows)}])
    p = parse.parse_order_bundles("synthetic", [rec], known_items, {"syn-aurora"})
    assert p.valid.height == 4 and p.quarantine.is_empty()
    assert p.valid.filter(pl.col("order_id") == 1)["price"][0] == 4.5
    assert p.coerced == 2


def test_duplicates_keep_first_copy_and_quarantine_the_rest(tmp_path, known_items):
    rec = _bundle(RawStore(tmp_path), [{"type_id": 34, "status": 200, "body": json.dumps([_order(), _order()])}])
    p = parse.parse_order_bundles("synthetic", [rec], known_items, {"syn-aurora"})
    assert p.valid.height == 1 and p.quarantine["reason"].to_list() == ["duplicate"]


def test_fetch_failures_and_truncated_bodies_become_explicit_gaps(tmp_path, known_items):
    rec = _bundle(
        RawStore(tmp_path),
        [{"type_id": 34, "status": 502, "body": None}, {"type_id": 35, "status": 200, "body": '[{"order_id": 1, "pri'}],
    )
    p = parse.parse_order_bundles("synthetic", [rec], known_items, {"syn-aurora"})
    assert sorted(p.gaps["item_id"].to_list()) == [34, 35]
    assert p.quarantine["reason"].to_list() == ["unparseable_body"]


def test_unknown_server_and_corrupt_bundle_are_quarantined_whole(tmp_path, known_items):
    store = RawStore(tmp_path)
    a = _bundle(store, [], server="syn-unknown")
    ref = store.put(
        source="synthetic",
        kind="orders",
        day=T.date(),
        key="syn-aurora_x",
        body="{not json",
        fetched_at=T,
        observed_at=T,
        meta={"server_id": "syn-aurora"},
    )
    p = parse.parse_order_bundles("synthetic", [a, store.read(ref)], known_items, {"syn-aurora"})
    assert sorted(p.quarantine["reason"].to_list()) == ["corrupt_payload", "unknown_server"]


def test_history_latest_fetch_wins_and_inconsistent_rows_are_quarantined(tmp_path, known_items):
    store = RawStore(tmp_path)
    day1 = [{"date": "2026-01-01", "average": 5.0, "highest": 6.0, "lowest": 4.0, "volume": 10, "order_count": 2}]
    revised = [
        {**day1[0], "average": 5.5},
        {"date": "2026-01-02", "average": 9.0, "highest": 6.0, "lowest": 4.0, "volume": 10, "order_count": 2},
    ]
    recs = []
    for k, body in enumerate([day1, revised]):
        t = datetime(2026, 1, 2 + k, tzinfo=UTC)
        ref = store.put(
            source="eve",
            kind="market_history",
            day=t.date(),
            key="eve-domain_34",
            body=json.dumps(body),
            fetched_at=t,
            observed_at=t,
            meta={"server_id": "eve-domain", "type_id": 34},
        )
        recs.append(store.read(ref))
    p = parse.parse_history(recs, known_items, {"eve-domain"})
    assert p.valid["average"].to_list() == [5.5]  # revision applied, deterministic
    assert p.quarantine["reason"].to_list() == ["inconsistent_range"]
    p_asof = parse.parse_history(recs, known_items, {"eve-domain"}, as_of=datetime(2026, 1, 2, tzinfo=UTC))
    assert p_asof.valid["average"].to_list() == [5.0]  # an as-of view reproduces what was known then


def test_patch_rss_sections_become_dated_events():
    rss = synthetic.render_rss(small_config(), list(synthetic.PATCHES[:3]))
    events = parse.parse_patch_rss(rss)
    assert [e.released_at for e in events] == [
        datetime(2025, 9, 13, 11, tzinfo=UTC),
        datetime(2025, 10, 16, 11, tzinfo=UTC),
        datetime(2025, 11, 11, 11, tzinfo=UTC),
    ]
    assert "ore yields increased" in events[1].notes


def test_real_patch_feed_fixture_parses():
    rss = (REFERENCE / "fixtures" / "eve_patch_rss_sample.xml").read_text(encoding="utf-8")
    events = parse.parse_patch_rss(rss)
    assert len(events) >= 2 and all(e.released_at.tzinfo is not None for e in events)


# ------------------------------------------------------------------------------------------- raw store
def test_raw_store_is_content_addressed_and_idempotent(tmp_path):
    store = RawStore(tmp_path)
    kw = dict(source="eve", kind="orders", day=date(2026, 1, 1), key="k", body="[]", fetched_at=T, observed_at=T)
    a, b = store.put(**kw), store.put(**kw)
    assert a.path == b.path and len(list(tmp_path.rglob("*.json.gz"))) == 1
    assert store.put(**{**kw, "body": "[1]"}).path != a.path  # new content never overwrites old


def test_raw_store_detects_tampering(tmp_path):
    store = RawStore(tmp_path)
    ref = store.put(source="eve", kind="orders", day=date(2026, 1, 1), key="k", body="[]", fetched_at=T, observed_at=T)
    os.chmod(ref.path, 0o644)
    with gzip.open(ref.path, "wb") as fh:
        fh.write(b'{"body": "[999]"}')
    with pytest.raises(ValueError, match="corrupted"):
        store.read(ref)


@pytest.mark.parametrize("bad", ["../etc", "a/b", "", ".hidden", "x y"])
def test_raw_store_rejects_unsafe_path_components(tmp_path, bad):
    with pytest.raises(ValueError):
        RawStore(tmp_path).put(
            source=bad, kind="k", day=date(2026, 1, 1), key="k", body="", fetched_at=T, observed_at=T
        )


# ------------------------------------------------------------------------------------------- generator
def test_generator_is_deterministic_and_ticks_extend_the_same_history(tmp_path):
    cfg = synthetic.SynthConfig(start=date(2025, 9, 1), servers=1, seed=3, items=(34, 587))
    full, part = RawStore(tmp_path / "full"), RawStore(tmp_path / "part")
    synthetic.generate(cfg, full, REFERENCE, until=datetime(2025, 9, 12, tzinfo=UTC))
    synthetic.generate(cfg, part, REFERENCE, until=datetime(2025, 9, 6, tzinfo=UTC))
    synthetic.generate(cfg, part, REFERENCE, until=datetime(2025, 9, 12, tzinfo=UTC))

    def files(root: Path):
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.json.gz"))

    assert files(tmp_path / "full") == files(tmp_path / "part")


def test_generated_feed_contains_the_defects_the_real_feed_has(small_world, known_items):
    store = small_world["store"]
    recs = [store.read(r) for d in store.days("synthetic", "orders") for r in store.iter_refs("synthetic", "orders", d)]
    p = parse.parse_order_bundles("synthetic", recs, known_items, set(small_world["cfg"].server_ids))
    assert {"duplicate", "missing_field", "non_positive_price", "type_mismatch"} <= set(p.reasons)
    assert p.coerced > 0 and p.gaps.height > 0
    assert p.quarantine.height < 0.01 * p.valid.height  # defects are rare, as in reality
    late = [r for r in recs if r.fetched_at - r.observed_at > timedelta(hours=12)]
    assert late, "some snapshots must arrive late"


# ------------------------------------------------------------------------------------------- fills
def test_fill_inference_tracks_ground_truth_volume(small_world, known_items):
    """Differential test against the simulator's true fills (docs/PERFORMANCE.md reports the error)."""
    store, root = small_world["store"], small_world["root"]
    recs = [store.read(r) for d in store.days("synthetic", "orders") for r in store.iter_refs("synthetic", "orders", d)]
    listings = parse.parse_order_bundles("synthetic", recs, known_items, set(small_world["cfg"].server_ids)).valid
    inferred = (
        infer_fills(listings)
        .with_columns(day=pl.col("snapshot_ts").dt.date())
        .group_by("server_id", "item_id", "day")
        .agg(pl.col("fill_qty").sum())
    )
    truth = pl.read_parquet(root / "truth" / "fills.parquet")
    j = truth.join(inferred, on=["server_id", "item_id", "day"], how="inner").filter(pl.col("day") > date(2025, 9, 2))
    ratio = j["fill_qty"].sum() / j["qty"].sum()
    corr = j.select(pl.corr(pl.col("qty").log1p(), pl.col("fill_qty").log1p()))[0, 0]
    assert 0.8 < ratio < 1.25, ratio
    assert corr > 0.9, corr


def test_history_staging_reads_only_the_latest_fetch_and_keeps_aged_out_days(tmp_path):
    from goldstandard.config import Settings
    from goldstandard.pipeline import Pipeline

    pipe = Pipeline(Settings(data_dir=tmp_path))
    row = {"highest": 6.0, "lowest": 4.0, "volume": 10, "order_count": 2}

    def fetch(day: date, rows: list[dict]) -> None:
        t = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
        pipe.store.put(
            source="eve",
            kind="market_history",
            day=day,
            key="eve-domain_34",
            body=json.dumps(rows),
            fetched_at=t,
            observed_at=t,
            meta={"server_id": "eve-domain", "type_id": 34},
        )

    fetch(
        date(2026, 1, 3), [{"date": "2026-01-01", "average": 5.0, **row}, {"date": "2026-01-02", "average": 5.0, **row}]
    )
    assert pipe.stage_history() == [date(2026, 1, 1), date(2026, 1, 2)]
    # a later fetch no longer contains Jan 1 (aged out) and revises Jan 2
    fetch(
        date(2026, 1, 4), [{"date": "2026-01-02", "average": 5.5, **row}, {"date": "2026-01-03", "average": 6.0, **row}]
    )
    assert pipe.stage_history() == [date(2026, 1, 2), date(2026, 1, 3)]
    assert pipe.lake.read_day("history", "eve", date(2026, 1, 1))["average"].to_list() == [5.0]  # kept
    assert pipe.lake.read_day("history", "eve", date(2026, 1, 2))["average"].to_list() == [5.5]  # latest wins
    assert pipe.stage_history() == []  # nothing changed: nothing rewritten
