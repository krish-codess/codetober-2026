"""Runner logic, plus assertions over one real end-to-end run (the `report` fixture)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest

from tydlc.engine import Engine
from tydlc.properties import Property
from tydlc.runner import CRASH, _drop_rows, _without, confidence, evaluate, shrink
from tydlc.schema import Dataset
from tydlc.subjects import Subject
from tydlc.subjects.jaffle import SCHEMA

ONE_ORDER_NO_PAYMENT: Dataset = {
    "raw_customers": [{"id": 0, "first_name": None, "last_name": None}],
    "raw_orders": [{"id": 0, "user_id": 0, "order_date": None, "status": "placed"}],
    "raw_payments": [],
}


def _by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in report["properties"]}


def test_confidence_is_the_rule_of_three() -> None:
    assert confidence(0, 0) == 0.0          # never exercised: no evidence
    assert confidence(2, 0) == 0.0          # too few to say anything
    assert confidence(300, 0) == pytest.approx(0.99)
    assert confidence(300, 1) == 0.0        # one counterexample ends it
    assert confidence(30, 0) < confidence(300, 0) < 1


def _ds() -> Dataset:
    return {
        "raw_customers": [{"id": 1}, {"id": 2}],
        "raw_orders": [{"id": 10, "user_id": 1}, {"id": 11, "user_id": 2}],
        "raw_payments": [{"id": 100, "order_id": 10}, {"id": 101, "order_id": 11}],
    }


def test_removing_a_parent_cascades_to_its_descendants() -> None:
    out = _without(SCHEMA, _ds(), "raw_customers", 0)
    assert out == {"raw_customers": [{"id": 2}], "raw_orders": [{"id": 11, "user_id": 2}],
                   "raw_payments": [{"id": 101, "order_id": 11}]}


def test_drop_rows_reaches_a_row_minimal_dataset() -> None:
    minimal = _drop_rows(SCHEMA, _ds(), lambda ds: any(p["id"] == 101 for p in ds["raw_payments"]))
    assert minimal == {"raw_customers": [{"id": 2}], "raw_orders": [{"id": 11, "user_id": 2}],
                       "raw_payments": [{"id": 101, "order_id": 11}]}


def test_engine_error_is_reported_as_a_crash_not_raised(duck: Engine) -> None:
    bad: Dataset = {**ONE_ORDER_NO_PAYMENT,
                    "raw_customers": [{"id": "not a number", "first_name": None,
                                       "last_name": None}]}
    result = evaluate(duck, [], bad)
    assert result == {CRASH.name: False}
    assert evaluate(duck, [], ONE_ORDER_NO_PAYMENT) == {CRASH.name: True}  # engine still usable


def test_a_raising_check_counts_as_a_violation(duck: Engine) -> None:
    boom = Property("boom", "raises", lambda c: 1 / 0 > 0)
    assert evaluate(duck, [boom], ONE_ORDER_NO_PAYMENT)["boom"] is False


def test_shrink_returns_none_when_nothing_fails(subject: Subject, duck: Engine) -> None:
    always = Property("always", "holds", lambda c: True)
    minimal, calls, _ = shrink(subject, duck, always, seed=1, max_examples=20)
    assert minimal is None and calls == 20


# --- one real run -----------------------------------------------------------------------

def test_finds_the_real_bugs_and_only_those(report: dict[str, Any]) -> None:
    declared = {n: p for n, p in _by_name(report).items() if p["source"] == "declared"}
    falsified = {n for n, p in declared.items() if p["status"] == "falsified"}
    assert falsified == {"orders_amount_not_null", "orders_method_amounts_not_null",
                         "row_order_invariant"}
    assert report["unexpected_failures"] == [] and report["stale_known_bugs"] == []
    assert report["ok"] is True
    assert all(declared[n]["known_bug"] for n in falsified)


def test_failure_is_shrunk_to_the_minimal_dataset(report: dict[str, Any]) -> None:
    failure = _by_name(report)["orders_amount_not_null"]["failure"]
    assert failure["shrunk"] is True
    assert failure["minimal_dataset"] == ONE_ORDER_NO_PAYMENT
    assert failure["minimal_rows"] == 2


def test_row_order_bug_needs_three_payments(report: dict[str, Any]) -> None:
    minimal = _by_name(report)["row_order_invariant"]["failure"]["minimal_dataset"]
    assert [len(minimal[t.name]) for t in SCHEMA] == [1, 1, 3]  # a + b + c, reassociated


def test_every_falsified_property_has_a_shrunk_counterexample(report: dict[str, Any]) -> None:
    for p in report["properties"]:
        assert (p["failure"] is not None) == (p["status"] == "falsified"), p["name"]
        if p["failure"]:
            assert p["failure"]["shrunk"] and p["failure"]["minimal_rows"] <= 6, p["name"]
            assert p["confidence"] == 0


def test_tallies_account_for_every_example(report: dict[str, Any]) -> None:
    assert report["examples"] == 40
    for p in report["properties"]:
        assert p["passed"] + p["failed"] + p["vacuous"] == 40, p["name"]


def test_discovery_hit_rate(report: dict[str, Any]) -> None:
    d = report["discovery"]
    found = [p for p in report["properties"] if p["source"] == "discovered"]
    assert d["candidates"] == len(found) > 50
    assert d["held"] == sum(p["status"] == "held" for p in found)
    assert d["hit_rate"] == pytest.approx(d["held"] / d["candidates"], abs=1e-4)
    assert 0 < d["hit_rate"] < 1  # some candidates are artefacts of the clean sample
    by_name = _by_name(report)
    assert by_name["not_null(orders.amount)"]["status"] == "falsified"       # the real bug
    assert by_name["row_count_eq(orders,raw_orders)"]["status"] == "held"    # a real invariant
    assert by_name["sum_eq(orders.amount,stg_payments.amount)"]["status"] == "held"


def test_outcomes_parquet_matches_the_report(report: dict[str, Any], data_dir: Path) -> None:
    path = str(data_dir / "runs" / "session" / "outcomes.parquet")
    con = duckdb.connect()
    total, properties, examples = con.execute(
        "SELECT count(*), count(DISTINCT property), count(DISTINCT example) "
        "FROM read_parquet(?)", [path]).fetchone() or (0, 0, 0)
    assert (properties, examples) == (len(report["properties"]), 40)
    assert total == properties * examples  # one verdict per (example, property), no gaps
    failed = con.execute("SELECT count(*) FROM read_parquet(?) WHERE property = ? "
                         "AND outcome = 'fail'", [path, "orders_amount_not_null"]).fetchone()
    assert failed and failed[0] == _by_name(report)["orders_amount_not_null"]["failed"]


def test_same_seed_reproduces_exactly_in_a_fresh_process(tmp_path: Path) -> None:
    """The CI contract: same code, same command, same seed -> same data, same verdicts,
    same minimal cases. Two separate interpreter processes, compared field by field."""
    def run(key: str) -> dict[str, Any]:
        subprocess.run(
            [sys.executable, "-m", "tydlc.cli", "--data-dir", str(tmp_path), "run",
             "--no-discover", "--max-examples", "30", "--seed", "5", "--run-key", key],
            check=True, capture_output=True)
        report = json.loads((tmp_path / "runs" / key / "report.json").read_text())
        return {"rows": report["rows_generated"],
                "properties": [(p["name"], p["passed"], p["failed"], p["vacuous"],
                                p["failure"] and p["failure"]["minimal_dataset"])
                               for p in report["properties"]]}

    first = run("first")
    assert first == run("second")
    assert first["rows"] > 0
