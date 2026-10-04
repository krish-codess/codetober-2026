"""The boundary: dirty input is quarantined with a reason, never dropped or coerced."""

from __future__ import annotations

import csv
from pathlib import Path

from tydlc.engine import Engine
from tydlc.ingest import ingest, load_quarantine, load_staged, profile
from tydlc.subjects import Subject
from tydlc.subjects.jaffle import SCHEMA

DIRTY = {
    "raw_customers.csv": (
        "id,first_name,last_name\n"
        "1,Ann,A.\n"
        "2,,\n"                 # names are nullable: valid
        "2,Dup,D.\n"            # duplicate id: the first occurrence wins
        "x,Bad,B.\n"            # id is not an integer
        ",NoId,N.\n"            # id missing
    ),
    "raw_orders.csv": (
        "id,user_id,order_date,status\n"
        "10,1,2018-01-01,placed\n"
        "11,1,01/02/2018,placed\n"      # inconsistent date format
        "12,1,2018-01-03,teleported\n"  # not an accepted value
        "13,99,2018-01-04,placed\n"     # customer 99 does not exist
        "14,1\n"                        # short row: status missing
        "15,2,,shipped\n"               # order_date is nullable: valid
    ),
    # No `amount` column at all: every row must be quarantined, not loaded as NULL.
    "raw_payments.csv": "id,order_id,payment_method\n100,10,coupon\n",
}


def _write(tmp_path: Path) -> Path:
    src = tmp_path / "raw"
    src.mkdir()
    for name, text in DIRTY.items():
        (src / name).write_text(text)
    return src


def test_dirty_rows_are_quarantined_with_reasons(tmp_path: Path) -> None:
    src, out = _write(tmp_path), tmp_path / "out"
    counts = ingest(SCHEMA, src, out)
    assert counts == {"raw_customers": {"staged": 2, "quarantined": 3},
                      "raw_orders": {"staged": 2, "quarantined": 4},
                      "raw_payments": {"staged": 0, "quarantined": 1}}
    staged = load_staged(SCHEMA, out)
    assert [c["id"] for c in staged["raw_customers"]] == [1, 2]
    assert staged["raw_customers"][1]["first_name"] is None
    assert [o["id"] for o in staged["raw_orders"]] == [10, 15]

    def reasons(table: str) -> list[str]:
        return [r["_reason"] for r in load_quarantine(table, out)]

    assert reasons("raw_customers") == ["id: duplicate", "id: not a int", "id: missing"]
    assert reasons("raw_orders") == ["order_date: not a date", "status: not an accepted value",
                                     "user_id: no such raw_customers.id", "status: missing"]
    assert reasons("raw_payments") == ["amount: missing"]
    # The raw text of a rejected row survives, so it can be fixed and replayed.
    assert load_quarantine("raw_orders", out)[0]["order_date"] == "01/02/2018"


def test_ingest_is_idempotent_and_leaves_raw_untouched(tmp_path: Path) -> None:
    src, out = _write(tmp_path), tmp_path / "out"
    before = {p.name: p.read_bytes() for p in src.iterdir()}
    first = ingest(SCHEMA, src, out)
    manifest = (out / "manifest.json").read_text()
    assert ingest(SCHEMA, src, out) == first
    assert (out / "manifest.json").read_text() == manifest
    assert {p.name: p.read_bytes() for p in src.iterdir()} == before


def test_late_parent_is_picked_up_on_rerun(tmp_path: Path) -> None:
    src, out = _write(tmp_path), tmp_path / "out"
    ingest(SCHEMA, src, out)
    with open(src / "raw_customers.csv", "a") as fh:
        fh.write("99,Late,L.\n")  # the customer order 13 was waiting for
    assert ingest(SCHEMA, src, out)["raw_orders"] == {"staged": 3, "quarantined": 3}


def test_real_seed_is_clean_and_pipeline_output_is_consistent(
        subject: Subject, duck: Engine, tmp_path: Path) -> None:
    """Data tests on the real sample: counts, uniqueness, integrity, and the money total."""
    assert ingest(SCHEMA, subject.seed_dir, tmp_path) == {
        "raw_customers": {"staged": 100, "quarantined": 0},
        "raw_orders": {"staged": 99, "quarantined": 0},
        "raw_payments": {"staged": 113, "quarantined": 0}}
    out = duck.run(load_staged(SCHEMA, tmp_path))
    assert (len(out["customers"]), len(out["orders"])) == (100, 99)
    order_ids = [o["order_id"] for o in out["orders"]]
    assert len(set(order_ids)) == 99
    assert {o["customer_id"] for o in out["orders"]} <= {c["customer_id"] for c in out["customers"]}
    with open(subject.seed_dir / "raw_payments.csv", newline="") as fh:
        cents = sum(int(row["amount"]) for row in csv.DictReader(fh))
    assert round(sum(o["amount"] for o in out["orders"]) * 100) == cents == 167200
    paying = [c["customer_lifetime_value"] for c in out["customers"]
              if c["customer_lifetime_value"] is not None]
    assert round(sum(paying) * 100) == cents and min(paying) >= 0


def test_profile_reports_every_column(subject: Subject) -> None:
    text = profile(SCHEMA, subject.seed_dir)
    assert "### raw_payments (113 rows)" in text
    for table in SCHEMA:
        for col in table.columns:
            assert f"| {col.name} |" in text
