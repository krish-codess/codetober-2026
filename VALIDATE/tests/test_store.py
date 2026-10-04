"""PostgreSQL integration: migrations both ways, constraints, idempotent writes, paging."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from tydlc import store

pytestmark = pytest.mark.integration


def _tables(conn: Any) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()}


def test_migrations_roll_forward_back_and_forward(db: Any) -> None:
    ours = {"runs", "properties", "property_results", "failures"}
    assert ours <= _tables(db) and store.current_version(db) == 1
    assert store.migrate(db, 0) == 0
    assert not ours & _tables(db) and store.current_version(db) == 0
    assert store.migrate(db) == 1 and ours <= _tables(db)
    assert store.migrate(db) == 1  # already current: nothing to do


@pytest.mark.parametrize("sql", [
    "INSERT INTO runs (idempotency_key, subject, engine, seed, max_examples)"
    " VALUES ('k', 'jaffle', 'sqlite', 1, 10)",                       # unknown engine
    "INSERT INTO runs (idempotency_key, subject, engine, seed, max_examples)"
    " VALUES ('k', 'jaffle', 'duckdb', 1, 0)",                        # no examples
    "INSERT INTO runs (idempotency_key, subject, engine, seed, max_examples, status)"
    " VALUES ('k', 'jaffle', 'duckdb', 1, 10, 'completed')",          # completed, no results
    "INSERT INTO runs (idempotency_key, subject, engine, seed, max_examples, status,"
    " finished_at) VALUES ('k', 'jaffle', 'duckdb', 1, 10, 'failed', now())",  # failed, no error
    "INSERT INTO property_results VALUES (999, 999, 1, 0, 0, 'held', 0.5, NULL)",  # no such run
])
def test_schema_rejects_invalid_rows(db: Any, sql: str) -> None:
    with pytest.raises(psycopg.errors.IntegrityError):
        db.execute(sql)


def test_result_constraints(db: Any, report: dict[str, Any]) -> None:
    run, _ = store.create_run(db, "constraints", "jaffle", "duckdb", 1, 10)
    store.finish_run(db, run["id"], report)
    for bad in ("UPDATE property_results SET confidence = 1.5",
                "UPDATE property_results SET status = 'held' WHERE failed > 0",
                "UPDATE property_results SET confidence = 0.9 WHERE status = 'falsified'",
                "UPDATE failures SET minimal_dataset = '[]'::json",
                "UPDATE runs SET candidates_held = candidates + 1"):
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(bad)


def test_create_run_is_idempotent(db: Any) -> None:
    first, created = store.create_run(db, "same-key", "jaffle", "duckdb", 1, 10)
    again, created_again = store.create_run(db, "same-key", "jaffle", "postgres", 2, 20)
    assert (created, created_again) == (True, False)
    assert again["id"] == first["id"] and again["engine"] == "duckdb"  # first write wins
    assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_finish_run_records_everything_once(db: Any, report: dict[str, Any]) -> None:
    run, _ = store.create_run(db, "finish", "jaffle", "duckdb", 28, 40)
    store.finish_run(db, run["id"], report)
    store.finish_run(db, run["id"], report)  # a retry must not duplicate or error
    saved = store.get_run(db, run["id"])
    assert saved and saved["status"] == "completed" and saved["gate_ok"] is True
    assert saved["candidates"] == report["discovery"]["candidates"]
    n_results = db.execute("SELECT count(*) FROM property_results").fetchone()[0]
    n_failures = db.execute("SELECT count(*) FROM failures").fetchone()[0]
    assert n_results == len(report["properties"])
    assert n_failures == sum(p["failure"] is not None for p in report["properties"])
    # Referential integrity of what was written.
    assert db.execute("SELECT count(*) FROM failures f LEFT JOIN property_results r"
                      " USING (run_id, property_id) WHERE r.run_id IS NULL").fetchone()[0] == 0


def test_fail_run_only_touches_a_running_run(db: Any, report: dict[str, Any]) -> None:
    run, _ = store.create_run(db, "to-fail", "jaffle", "duckdb", 1, 10)
    store.fail_run(db, run["id"], "boom")
    failed = store.get_run(db, run["id"])
    assert failed and (failed["status"], failed["error"]) == ("failed", "boom")
    store.finish_run(db, run["id"], report)  # too late: stays failed, records nothing
    after = store.get_run(db, run["id"])
    assert after and after["status"] == "failed"
    assert db.execute("SELECT count(*) FROM property_results").fetchone()[0] == 0


def test_abandoned_runs_are_failed(db: Any) -> None:
    old, _ = store.create_run(db, "old", "jaffle", "duckdb", 1, 10)
    fresh, _ = store.create_run(db, "fresh", "jaffle", "duckdb", 1, 10)
    db.execute("UPDATE runs SET started_at = now() - interval '2 hours' WHERE id = %s",
               (old["id"],))
    store.fail_abandoned_runs(db)
    assert store.get_run(db, old["id"])["status"] == "failed"  # type: ignore[index]
    assert store.get_run(db, fresh["id"])["status"] == "running"  # type: ignore[index]


def test_keyset_pagination_is_complete_and_stable(db: Any, report: dict[str, Any]) -> None:
    for i in range(7):
        store.create_run(db, f"page-{i}", "jaffle", "duckdb", i, 10)
    seen: list[int] = []
    cursor = None
    while True:
        page = store.list_runs(db, cursor, 3)
        seen += [r["id"] for r in page["items"]]
        if len(seen) == 3:  # a row inserted mid-scan must not shift or repeat later pages
            store.create_run(db, "page-late", "jaffle", "duckdb", 99, 10)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(seen, reverse=True) and len(seen) == len(set(seen)) == 7

    run_id = store.list_runs(db, None, 1)["items"][0]["id"]
    store.finish_run(db, run_id, report)
    names: list[str] = []
    cursor = None
    while True:
        page = store.catalog(db, run_id, cursor, 25)
        names += [i["name"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert names == sorted(p["name"] for p in report["properties"])


@pytest.mark.parametrize("cursor", ["not-base64!", "e30=", "ImEi"])  # garbage, {}, "a"
def test_bad_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(store.BadCursor):
        store.decode_cursor(cursor, int)


def test_analytics(db: Any, report: dict[str, Any]) -> None:
    for i in range(3):
        run, _ = store.create_run(db, f"a-{i}", "jaffle", "duckdb", i, 40)
        store.finish_run(db, run["id"], report)
    freq = {r["name"]: r for r in store.failure_frequency(db, "duckdb", 20)}
    bug = next(p for p in report["properties"] if p["name"] == "orders_amount_not_null")
    row = freq["orders_amount_not_null"]
    assert (row["runs"], row["falsified_runs"]) == (3, 3)
    assert row["failed_examples"] == 3 * bug["failed"]
    assert row["failure_frequency"] == pytest.approx(bug["failure_frequency"], abs=1e-3)
    assert "cents_conserved" not in freq  # never failed on duckdb
    assert store.failure_frequency(db, "postgres", 20) == []
    rates = store.discovery_hit_rate(db, "duckdb", 2)
    assert len(rates) == 2
    assert rates[0]["hit_rate"] == pytest.approx(report["discovery"]["hit_rate"], abs=1e-3)


def test_connect_backs_off_with_a_cap_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure injection: nothing listens on this port."""
    delays: list[float] = []
    monkeypatch.setattr(store.time, "sleep", delays.append)
    with pytest.raises(psycopg.OperationalError):
        store.connect("postgresql://nobody@127.0.0.1:1/none", attempts=6)
    assert delays == [0.5, 1.0, 2.0, 4.0, 8.0]
