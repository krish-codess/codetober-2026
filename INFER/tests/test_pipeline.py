"""The pipeline against a real PostgreSQL: data tests, idempotency, failure injection, privileges, migrations.

A pipeline that runs green and writes wrong data must fail here.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tests.conftest import alembic, temporary_database
from uplift import db, ingest, learners, synth, train
from uplift.config import Settings

pytestmark = pytest.mark.integration

# Other test modules add their own datasets to the same database; these scope a query to the session's main one.
SYNTHETIC = "dataset_id = (SELECT id FROM dataset WHERE name = 'synthetic')"
SERVED = "experiment_id = (SELECT e.id FROM experiment e JOIN dataset d ON d.id = e.dataset_id WHERE d.name = 'synthetic' AND e.status = 'complete' ORDER BY e.finished_at DESC LIMIT 1)"


def one(conn: psycopg.Connection[Any], sql: str, *params: object) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return next(iter(row.values()))


# ---------------------------------------------------------------- data tests


def test_every_raw_line_is_loaded_or_quarantined(trained: dict[str, Any], owner: psycopg.Connection[Any]) -> None:
    m = trained["manifest"]
    assert (
        one(
            owner,
            "SELECT sum(rows_total) FROM raw_file f JOIN dataset d ON d.id = f.dataset_id WHERE d.name = 'synthetic'",
        )
        == m["rows_main"] + m["rows_late"]
    )
    units = one(owner, "SELECT count(*) FROM unit u JOIN dataset d ON d.id = u.dataset_id WHERE d.name = 'synthetic'")
    assert units == one(
        owner,
        "SELECT sum(rows_loaded) FROM raw_file f JOIN dataset d ON d.id = f.dataset_id WHERE d.name = 'synthetic'",
    )
    # exactly the planted defects are missing: invalid rows and units recorded in two arms. Late outcomes arrived.
    assert units == m["units"] - m["invalid"] - m["conflicting_duplicate"]
    quarantined = one(
        owner,
        "SELECT count(*) FROM quarantine q JOIN raw_file f ON f.sha256 = q.raw_sha JOIN dataset d ON d.id = f.dataset_id WHERE d.name = 'synthetic'",
    )
    assert quarantined == m["invalid"] + m["late_outcome"] + m["duplicate_row"] + 2 * m["conflicting_duplicate"]


def test_units_are_unique_and_rejected_rows_never_leak_in(
    trained: dict[str, Any], owner: psycopg.Connection[Any]
) -> None:
    assert one(owner, "SELECT count(*) - count(DISTINCT (dataset_id, unit_id)) FROM unit") == 0
    leaked = one(
        owner,
        "SELECT count(*) FROM quarantine q JOIN raw_file f ON f.sha256 = q.raw_sha JOIN unit u ON u.dataset_id = f.dataset_id AND u.unit_id = q.unit_id"
        " WHERE q.reason LIKE 'invalid_%%' OR q.reason = 'conflicting_duplicate'",
    )
    assert leaked == 0
    # a quarantined row keeps its raw content, so it can be repaired and replayed
    raw = one(owner, "SELECT raw FROM quarantine WHERE reason = 'invalid_segment' LIMIT 1")
    assert raw["segment"] == "Test E-Mail" and raw["customer_id"].startswith("C")


def test_late_outcomes_complete_their_units(trained: dict[str, Any], owner: psycopg.Connection[Any]) -> None:
    late_sha = trained["late"]["sha256"]
    assert one(owner, "SELECT count(*) FROM unit WHERE raw_sha = %s", late_sha) == trained["manifest"]["late_outcome"]
    waiting = one(
        owner,
        "SELECT count(*) FROM quarantine q WHERE q.reason = 'missing_outcome' AND NOT EXISTS (SELECT 1 FROM unit u WHERE u.unit_id = q.unit_id)",
    )
    assert waiting == 0


def test_distribution_invariants(trained: dict[str, Any], owner: psycopg.Connection[Any]) -> None:
    row = owner.execute(
        "SELECT avg(treatment::int) AS treated, avg(outcome) AS outcome, avg(true_uplift) AS uplift, min(recency) AS rmin, max(recency) AS rmax,"
        " min(history) AS hmin, avg((history IS NULL)::int) AS history_missing, count(DISTINCT zip_code) AS zips, count(DISTINCT channel) AS channels FROM unit WHERE "
        + SYNTHETIC
    ).fetchone()
    assert row is not None
    assert abs(row["treated"] - Decimal(2) / 3) < Decimal("0.01")  # the design's 2:1 allocation survived ingestion
    assert 0.10 < row["outcome"] < 0.22
    assert abs(row["uplift"] - trained["manifest"]["mean_true_uplift"]) < 0.002
    assert (row["rmin"], row["rmax"], row["zips"], row["channels"]) == (
        1,
        12,
        3,
        3,
    )  # format variants collapsed to 3 + 3
    assert row["hmin"] == Decimal("29.99")
    assert 0.02 < row["history_missing"] < 0.04  # missing features are kept as NULL, at the planted rate


def test_ingest_is_idempotent(trained: dict[str, Any], settings: Settings, owner: psycopg.Connection[Any]) -> None:
    before = one(owner, "SELECT count(*) FROM unit"), one(owner, "SELECT count(*) FROM quarantine")
    again = ingest.ingest(settings, trained["dir"] / "synthetic.csv", "synthetic")
    assert again["skipped"] is True
    assert (one(owner, "SELECT count(*) FROM unit"), one(owner, "SELECT count(*) FROM quarantine")) == before


def test_raw_files_are_preserved_by_content_hash(trained: dict[str, Any], settings: Settings) -> None:
    preserved = settings.raw_dir / f"{trained['main']['sha256']}.csv"
    assert ingest.sha256_file(preserved) == trained["main"]["sha256"]
    assert preserved.read_bytes() == (trained["dir"] / "synthetic.csv").read_bytes()


# ---------------------------------------------------------------- experiment results


def test_split_is_temporal_and_60_20_20(trained: dict[str, Any], owner: psycopg.Connection[Any]) -> None:
    assert trained["summary"]["split"] == "temporal"
    rows = owner.execute(
        "SELECT s.split, count(*) AS n, min(u.assigned_at) AS first, max(u.assigned_at) AS last FROM score s"
        " JOIN unit u USING (dataset_id, unit_id) WHERE s." + SYNTHETIC + " GROUP BY s.split"
    ).fetchall()
    by = {r["split"]: r for r in rows}
    total = sum(r["n"] for r in rows)
    assert [round(by[k]["n"] / total, 2) for k in ("train", "val", "test")] == [0.6, 0.2, 0.2]
    assert (
        by["train"]["last"] <= by["val"]["first"] <= by["val"]["last"] <= by["test"]["first"]
    )  # no peeking at the future


def test_every_unit_is_scored_once_and_rescoring_changes_nothing(
    trained: dict[str, Any], settings: Settings, owner: psycopg.Connection[Any]
) -> None:
    assert trained["scored"]["rows"] == one(owner, "SELECT count(*) FROM unit WHERE " + SYNTHETIC)
    fingerprint = (
        "SELECT count(*) || ':' || md5(string_agg(unit_id || '=' || round(uplift::numeric, 9)::text, ',' ORDER BY run_id, unit_id)) FROM score WHERE "
        + SYNTHETIC
    )
    # 9 decimals: the forest sums trees on two threads, so its predictions are reproducible to ~1e-13, not bit for bit
    before = one(owner, fingerprint)
    train.score(settings, "synthetic")
    assert one(owner, fingerprint) == before
    assert (
        one(owner, "SELECT count(*) FROM score s LEFT JOIN unit u USING (dataset_id, unit_id) WHERE u.unit_id IS NULL")
        == 0
    )


def test_evaluation_gate_on_the_full_pipeline(trained: dict[str, Any], owner: psycopg.Connection[Any]) -> None:
    runs = {r["learner"]: r for r in owner.execute("SELECT * FROM model_run WHERE " + SERVED).fetchall()}
    assert set(runs) == set(learners.LEARNERS) and all(r["status"] == "complete" for r in runs.values())
    champion = next(r for r in runs.values() if r["is_champion"])
    assert champion["metrics"]["estimates_uplift"] and champion["learner"] == trained["summary"]["champion"]
    test = champion["metrics"]["test"]
    assert test["qini_share_of_oracle"] > 0.5, (
        "the champion lost most of the achievable Qini on data with known effects"
    )
    assert test["qini"] > runs["response"]["metrics"]["test"]["qini"] + 0.004, "no better than targeting likely buyers"
    assert test["qini_ci95"][0] > 0
    assert test["p_beats_response"] > 0.75  # on 8k held-out rows; the strict gate is tests/test_learners.py
    assert test["pehe"] < 0.04
    assert 0.5 < champion["metrics"]["calibration_slope"] < 1.6  # predicted uplift means what it says, decile by decile
    assert one(owner, "SELECT count(*) FROM decile WHERE run_id = %s", champion["id"]) == 10


def test_the_planted_harm_is_found_twice(trained: dict[str, Any], owner: psycopg.Connection[Any]) -> None:
    # model-free: the scan names the segment the generator hurts
    hurts = [
        r["definition"]
        for r in owner.execute("SELECT definition FROM segment WHERE verdict = 'hurts' AND " + SERVED).fetchall()
    ]
    assert "history = $350 and over AND channel = Phone" in hurts
    assert all("Phone" in h for h in hurts), f"segments flagged as harmed that the generator does not harm: {hurts}"
    # model-based: the units the champion predicts are harmed really are (the generator knows), and the held-out
    # estimate for them agrees with the truth to within its own interval. With a few hundred held-out units the
    # interval may still straddle zero - then the verdict must be 'unclear', never 'helps'.
    profiles = one(owner, "SELECT profiles FROM model_run WHERE is_champion AND " + SERVED)
    negative = profiles["negative_uplift"]
    assert negative["true_uplift"] < -0.02
    assert negative["ci_low"] - 0.02 <= negative["true_uplift"] <= negative["ci_high"] + 0.02
    assert negative["verdict"] in ("hurts", "unclear")
    assert (negative["verdict"] == "hurts") == (negative["ci_high"] < 0)
    assert {"feature": "channel", "level": "Phone"} in [
        {"feature": t["feature"], "level": t["level"]} for t in negative["traits"][:3]
    ]
    assert profiles["high_uplift"]["verdict"] == "helps"


def test_a_tampered_artifact_is_refused(
    trained: dict[str, Any], settings: Settings, owner: psycopg.Connection[Any], tmp_path: Path
) -> None:
    run = owner.execute(
        "SELECT artifact_file, artifact_sha256 FROM model_run WHERE learner = 'random' AND " + SERVED
    ).fetchone()
    assert run is not None
    assert train.load_artifact(settings, run["artifact_file"], run["artifact_sha256"])["learner"] == "random"
    elsewhere = dataclasses.replace(settings, data_dir=tmp_path)
    target = elsewhere.artifact_dir / run["artifact_file"]
    target.parent.mkdir(parents=True)
    target.write_bytes((settings.artifact_dir / run["artifact_file"]).read_bytes() + b"\x00")
    with pytest.raises(ValueError, match="does not match its recorded SHA-256"):
        train.load_artifact(elsewhere, run["artifact_file"], run["artifact_sha256"])


# ---------------------------------------------------------------- failure injection


@pytest.fixture(scope="module")
def small(settings: Settings) -> dict[str, Any]:
    """A second, small dataset for the tests that break things."""
    out = settings.data_dir / "small"
    manifest = synth.generate(out, 7000, seed=99)
    ingest.ingest(settings, out / "synthetic.csv", "small")
    return {"dir": out, "manifest": manifest}


def test_one_learner_failing_does_not_cost_the_others(
    small: dict[str, Any], settings: Settings, owner: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(learners.SLearner):
        def fit(self, X: Any, t: Any, y: Any) -> Any:
            raise MemoryError("injected: out of memory")

    monkeypatch.setitem(learners.LEARNERS, "s_learner", Broken)
    summary = train.train(settings, "small", ["response", "s_learner", "t_learner"], rounds=10)
    assert summary["failed"] == ["s_learner"] and summary["champion"] == "t_learner"
    row = owner.execute(
        "SELECT status, error, metrics FROM model_run WHERE experiment_id = %s AND learner = 's_learner'",
        [summary["experiment"]],
    ).fetchone()
    assert row == {"status": "failed", "error": "MemoryError: injected: out of memory", "metrics": None}
    assert one(owner, "SELECT status FROM experiment WHERE id = %s", summary["experiment"]) == "complete"


def test_broken_randomization_is_refused_not_modelled(
    small: dict[str, Any], settings: Settings, owner: psycopg.Connection[Any]
) -> None:
    served_before = one(owner, "SELECT count(*) FROM experiment WHERE status = 'complete'")
    with pytest.raises(SystemExit, match="training refused: sample ratio mismatch"):
        train.train(settings, "small", ["t_learner"], treated_share=0.5)  # the data is 2:1; claim the design was 1:1
    failed = owner.execute("SELECT status, audit FROM experiment ORDER BY started_at DESC LIMIT 1").fetchone()
    assert failed is not None and failed["status"] == "failed"
    assert failed["audit"]["srm_p_value"] < 0.001 and "sample ratio mismatch" in failed["audit"]["error"]
    assert one(owner, "SELECT count(*) FROM experiment WHERE status = 'complete'") == served_before


def test_new_data_makes_the_model_stale_and_scoring_refuses(small: dict[str, Any], settings: Settings) -> None:
    train.train(settings, "small", ["t_learner"], rounds=10)
    train.score(settings, "small")
    ingest.ingest(settings, small["dir"] / "synthetic_late.csv", "small")  # a late file lands after training
    with pytest.raises(SystemExit, match="retrain before scoring"):
        train.score(settings, "small")


def test_a_crash_mid_ingest_leaves_nothing_behind(
    settings: Settings, owner: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    out = settings.data_dir / "crash"
    synth.generate(out, 3000, seed=5)
    real, calls = ingest._raw_rows, {"n": 0}

    def dies_after_units_are_inserted(path: Path, wanted: set[int]) -> dict[int, str]:
        calls["n"] += 1
        if calls["n"] == 2:  # the first call is in parse(); the second is after INSERT INTO unit
            raise RuntimeError("injected: process killed")
        return real(path, wanted)

    monkeypatch.setattr(ingest, "_raw_rows", dies_after_units_are_inserted)
    with pytest.raises(RuntimeError, match="injected"):
        ingest.ingest(settings, out / "synthetic.csv", "crash")
    assert one(owner, "SELECT count(*) FROM dataset WHERE name = 'crash'") == 0
    assert one(owner, "SELECT count(*) FROM raw_file WHERE filename = 'synthetic.csv' AND rows_total < 4000") == 0
    monkeypatch.undo()
    done = ingest.ingest(settings, out / "synthetic.csv", "crash")  # the retry is a clean first load
    assert (
        done["rows_loaded"]
        == one(owner, "SELECT count(*) FROM unit u JOIN dataset d ON d.id = u.dataset_id WHERE d.name = 'crash'")
        > 2900
    )


def test_an_unreachable_database_is_retried_with_capped_backoff_then_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    with pytest.raises(SystemExit, match="database unreachable after 4 attempts"):
        db.connect("postgresql://nobody@127.0.0.1:9/none", delays=(0.1, 0.2, 0.4))
    assert slept == [0.1, 0.2, 0.4]


# ---------------------------------------------------------------- constraints, privileges, migrations


def test_the_budget_guarantee_is_also_a_database_constraint(
    trained: dict[str, Any], owner: psycopg.Connection[Any]
) -> None:
    run_id = one(owner, "SELECT id FROM model_run WHERE is_champion LIMIT 1")
    insert = (
        "INSERT INTO policy (id, run_id, idempotency_key, budget, cost_per_treatment, value_per_outcome, population, n_targeted, evaluation)"
        " VALUES (gen_random_uuid(), %s, %s, 100, 1, 5, 1000, %s, '{}')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute(insert, [run_id, "over-budget-1", 101])
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute(insert.replace("1000", "50"), [run_id, "over-population", 60])


@pytest.mark.parametrize(
    ("role", "statement", "allowed"),
    [
        ("api", "SELECT count(*) FROM unit", True),
        ("api", "UPDATE unit SET outcome = 1", False),
        ("api", "DELETE FROM score", False),
        ("api", "UPDATE model_run SET is_champion = true", False),
        ("api", "DELETE FROM policy", False),
        ("batch", "UPDATE experiment SET timings = timings", True),
        ("batch", "DROP TABLE policy", False),
        ("batch", "CREATE TABLE sneaky (x int)", False),
        ("batch", "ALTER TABLE unit DROP CONSTRAINT unit_outcome_check", False),
    ],
)
def test_least_privilege(database: dict[str, str], role: str, statement: str, allowed: bool) -> None:
    with psycopg.connect(database[role]) as conn:
        if allowed:
            conn.execute(statement)
        else:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
        conn.rollback()


def test_migrations_roll_back_and_forward() -> None:
    tables = "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
    with temporary_database(migrate=False) as urls, psycopg.connect(urls["owner"], autocommit=True) as conn:
        alembic(urls["owner"], "upgrade", "head")
        assert conn.execute(tables).fetchone() == (10,)
        alembic(urls["owner"], "downgrade", "-1")  # 0002: grants only
        assert conn.execute(tables).fetchone() == (10,)
        assert conn.execute("SELECT has_table_privilege('uplift_api_role', 'policy', 'INSERT')").fetchone() == (False,)
        alembic(urls["owner"], "downgrade", "base")
        assert conn.execute(tables).fetchone() == (0,)
        alembic(urls["owner"], "upgrade", "head")
        assert conn.execute(tables).fetchone() == (10,)
        assert conn.execute("SELECT has_table_privilege('uplift_api_role', 'policy', 'INSERT')").fetchone() == (True,)
