"""The batch, end to end on a seeded year: ingest reconciles, late data lands, and nothing published is untrue."""

from __future__ import annotations

import dataclasses
import gzip
import json
import shutil
from pathlib import Path

import duckdb
import pytest

from wrapped import cards
from wrapped.build import audit, build
from wrapped.cli import transform
from wrapped.config import Settings
from wrapped.ingest import fetch, ingest

ROOT = Path(__file__).resolve().parent.parent


def q(settings: Settings, sql: str) -> list[tuple]:
    con = duckdb.connect(str(settings.warehouse_path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def files(settings: Settings, sub: str) -> str:
    return (settings.data_dir / sub / "*.parquet").as_posix()


def test_quarantine_matches_the_defects_that_were_planted(pipeline: Settings) -> None:
    truth = json.loads((pipeline.raw_dir / "generator_truth.json").read_text())
    reasons = dict(
        duckdb.sql(f"SELECT reject_reason, count(*) FROM '{files(pipeline, 'quarantine')}' GROUP BY 1").fetchall()
    )
    planted = truth["defects"]
    assert reasons == {
        "malformed_json": planted["malformed_json"],
        "missing_event_id": planted["missing_event_id"],
        "missing_actor": planted["missing_actor"],
        "unparseable_timestamp": planted["unparseable_timestamp"],
        "timestamp_out_of_range": planted["timestamp_in_future"],
    }
    lines, accepted, quarantined = duckdb.sql(
        f"SELECT sum(lines_read), sum(lines_accepted), sum(lines_quarantined) FROM '{files(pipeline, 'manifest')}'"
    ).fetchone()
    assert lines == truth["lines"] == accepted + quarantined, "every raw line is accounted for"
    rows, distinct = duckdb.sql(
        f"SELECT count(*), count(DISTINCT event_id) FROM '{files(pipeline, 'bronze')}'"
    ).fetchone()
    assert rows - distinct == planted["duplicate"], "bronze keeps redeliveries; dedup is a modelled step"
    assert q(pipeline, "SELECT count(*) FROM int_events__deduped")[0][0] == distinct


def test_timestamp_spellings_all_land_in_utc(pipeline: Settings) -> None:
    # The generator writes some timestamps as +00:00, with millis, or at -07:00. None may shift a day's count.
    assert q(pipeline, "SELECT count(*) FROM int_events__deduped WHERE occurred_at IS NULL")[0][0] == 0
    low, high = duckdb.sql(f"SELECT min(created_at), max(created_at) FROM '{files(pipeline, 'bronze')}'").fetchone()
    assert low.year == 2024 and high.year == 2025, "previous-year stragglers are kept in bronze"
    assert q(pipeline, "SELECT count(*) FROM mart_user_year WHERE year(first_event_at) <> 2025")[0][0] == 0


def test_automation_is_not_in_the_population(pipeline: Settings) -> None:
    assert q(pipeline, "SELECT count(*) FROM mart_user_year WHERE login LIKE '%[bot]'")[0][0] == 0
    assert q(pipeline, "SELECT count(*) FROM int_users WHERE is_labelled_bot AND NOT is_automated")[0][0] == 0
    assert q(pipeline, "SELECT automated_accounts FROM mart_population")[0][0] >= 4


def test_renamed_user_is_one_person_with_their_latest_login(pipeline: Settings) -> None:
    renamed = q(pipeline, "SELECT count(*) FROM mart_user_year WHERE login LIKE '%-dev'")[0][0]
    assert renamed >= 1
    assert q(pipeline, "SELECT count(*) - count(DISTINCT user_id) FROM mart_user_year")[0][0] == 0


def test_no_comparison_rests_on_fewer_than_k_people(pipeline: Settings) -> None:
    assert q(pipeline, "SELECT min(users_at_or_above) FROM mart_metric_cutpoints")[0][0] >= pipeline.k_anonymity
    assert q(pipeline, "SELECT min(users_at_or_above) FROM mart_user_ranks")[0][0] >= pipeline.k_anonymity


def test_every_published_claim_is_true(pipeline: Settings) -> None:
    result = audit(pipeline)
    assert result["violations"] == []
    assert result["payloads"] == q(pipeline, "SELECT count(*) FROM mart_user_year")[0][0]
    assert result["claims"] > 100, "the audit must actually have had claims to check"


def test_the_audit_catches_a_lie(pipeline: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Failure injection: a selector that rounds one rung in the user's favour must not survive the audit."""
    honest = cards.claim_permille

    def flattering(rank: cards.Rank | None) -> int | None:
        rung = honest(rank)
        if rung is None:
            return None
        return cards.LADDER_PERMILLE[max(0, cards.LADDER_PERMILLE.index(rung) - 1)]

    # Same warehouse, separate payload directory, so the honest run the other tests use is untouched.
    monkeypatch.setattr(Settings, "payload_dir", property(lambda self: tmp_path / "payloads"))
    monkeypatch.setattr(cards, "claim_permille", flattering)
    build(pipeline)
    monkeypatch.setattr(cards, "claim_permille", honest)
    result = audit(pipeline)
    rules = {v["rule"] for v in result["violations"]}
    assert result["violation_count"] > 0 and "claim" in rules and "reproducible" in rules


def test_stories_are_spread_out(pipeline: Settings) -> None:
    summary = build(pipeline)  # already built: returns the stored summary
    dist = summary["distribution"]
    assert summary.get("reused") is True
    assert dist["most_common_story_share"] < 0.25, "no single set of cards may dominate"
    assert dist["distinct_stories"] > 50
    assert len(dist["card_share"]) >= 15, "nearly the whole catalogue is in use"
    assert max(dist["card_share"].values()) < 0.85
    assert sum(summary["tiers"].values()) == summary["users"] and summary["tiers"]["minimal"] > 0


def test_rerunning_ingest_and_build_changes_nothing(pipeline: Settings) -> None:
    before = q(pipeline, "SELECT count(*), sum(events) FROM mart_user_year")
    assert ingest(pipeline)["files"] == 0
    assert build(pipeline)["reused"] is True
    assert q(pipeline, "SELECT count(*), sum(events) FROM mart_user_year") == before


def test_late_and_redelivered_events_are_counted_exactly_once(pipeline: Settings, tmp_path: Path) -> None:
    """A file arrives a week into the next year carrying one new 2025 event and one event we already have.

    The incremental run must add exactly one event to exactly one user, and agree with a full rebuild.
    Runs on a copy so the other tests keep their pipeline.
    """
    work = tmp_path / "late"
    shutil.copytree(pipeline.data_dir, work, ignore=shutil.ignore_patterns("tmp", "payloads", "dbt-*"))
    settings = dataclasses.replace(pipeline, data_dir=work)
    user_id, login, events = q(
        settings, "SELECT user_id, login, events FROM mart_user_year ORDER BY events DESC, user_id LIMIT 1"
    )[0]
    known = q(
        settings,
        f"SELECT event_id, occurred_at FROM int_events__deduped WHERE user_id = {user_id} ORDER BY event_id LIMIT 1",
    )[0]
    total = q(settings, "SELECT count(*) FROM int_events__deduped")[0][0]

    def event(event_id: int, created_at: str) -> str:
        return json.dumps({"id": str(event_id), "type": "WatchEvent", "actor": {"id": user_id, "login": login},
                           "repo": {"id": 900000001, "name": "org-302/core-1"}, "payload": {"action": "started"},
                           "public": True, "created_at": created_at})  # fmt: skip

    with gzip.open(settings.raw_dir / "2026-01-08-9.json.gz", "wt") as fh:
        fh.write(event(99_000_000_001, "2025-06-15T12:00:00Z") + "\n")
        fh.write(event(known[0], known[1].strftime("%Y-%m-%dT%H:%M:%SZ")) + "\n")

    assert ingest(settings)["accepted"] == 2
    transform(settings)  # incremental; its own reconciliation tests must pass
    assert q(settings, "SELECT count(*) FROM int_events__deduped")[0][0] == total + 1
    assert q(settings, f"SELECT events, stars FROM mart_user_year WHERE user_id = {user_id}")[0][0] == events + 1
    incremental = q(settings, "SELECT md5(string_agg(u::VARCHAR, '|' ORDER BY user_id)) FROM mart_user_year u")
    transform(settings, full_refresh=True)
    assert q(settings, "SELECT md5(string_agg(u::VARCHAR, '|' ORDER BY user_id)) FROM mart_user_year u") == incremental


def test_unreadable_file_is_rejected_whole_and_the_rest_still_loads(tmp_path: Path, pipeline: Settings) -> None:
    settings = dataclasses.replace(pipeline, data_dir=tmp_path)
    settings.raw_dir.mkdir()
    good = next(f for f in sorted(pipeline.raw_dir.glob("2025-03-*.json.gz")) if f.stat().st_size > 400)
    shutil.copy(good, settings.raw_dir / good.name)
    (settings.raw_dir / "2025-08-17-4.json.gz").write_bytes(
        good.read_bytes()[: good.stat().st_size // 2]
    )  # cut-off download
    (settings.raw_dir / "2025-08-18-4.json.gz").write_bytes(
        b"<?xml version='1.0'?><Error><Code>NoSuchKey</Code></Error>"
    )
    totals = ingest(settings)
    assert totals["files"] == 1 and totals["files_rejected"] == 2 and totals["accepted"] > 0
    statuses = dict(duckdb.sql(f"SELECT source_file, status FROM '{files(settings, 'manifest')}'").fetchall())
    assert statuses == {good.name: "ingested", "2025-08-17-4.json.gz": "rejected", "2025-08-18-4.json.gz": "rejected"}
    assert ingest(settings)["files_rejected"] == 0, "a rejected file is recorded, not retried forever"


def test_fetch_gives_up_after_capped_retries_and_leaves_nothing_behind(
    tmp_path: Path, pipeline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = dataclasses.replace(pipeline, data_dir=tmp_path)
    waits: list[float] = []
    monkeypatch.setattr("time.sleep", waits.append)
    with pytest.raises(RuntimeError, match="after 4 attempts"):
        fetch(settings, ["2025-03-12-05"], attempts=4, base_url="http://127.0.0.1:9")  # nothing listens on port 9
    assert waits == [2.0, 4.0, 8.0], "exponential backoff, and it stops"
    assert list(settings.raw_dir.iterdir()) == [], "no partial file is left to be mistaken for data"


def test_dbt_models_follow_the_layer_naming() -> None:
    prefixes = {"staging": "stg_", "intermediate": "int_", "marts": "mart_"}
    models = list((ROOT / "dbt" / "models").rglob("*.sql"))
    assert len(models) >= 10
    for model in models:
        assert model.name.startswith(prefixes[model.parent.name]), f"{model.name} is in {model.parent.name}/"
    documented = " ".join(p.read_text() for p in (ROOT / "dbt" / "models").rglob("_*.yml"))
    assert all(f"name: {m.stem}" in documented for m in models), "every model has tests and a description"
