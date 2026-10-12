"""The validation contract, row by row, and the generator's defects counted against what ingestion catches."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from uplift import synth
from uplift.ingest import Reject, normalize, parse
from uplift.profile import profile

GOOD = {
    "recency": "10",
    "history_segment": "2) $100 - $200",
    "history": "142.44",
    "mens": "1",
    "womens": "0",
    "zip_code": "Surburban",
    "newbie": "0",
    "channel": "Phone",
    "segment": "Womens E-Mail",
    "visit": "0",
    "conversion": "0",
    "spend": "0",
}


def test_a_clean_hillstrom_row() -> None:
    u = normalize(GOOD, "visit")
    assert u == {
        "treatment": True, "outcome": 0.0, "assigned_at": None, "recency": 10, "history": 142.44, "mens": True,
        "womens": False, "newbie": False, "zip_code": "Surburban", "channel": "Phone", "true_uplift": None,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("change", "field", "expected"),
    [
        ({"history": "$1,234.50"}, "history", 1234.5),
        ({"channel": " web "}, "channel", "Web"),
        ({"channel": "MULTICHANNEL"}, "channel", "Multichannel"),
        ({"newbie": "Y"}, "newbie", True),
        ({"newbie": "false"}, "newbie", False),
        ({"segment": "no e-mail"}, "treatment", False),
        ({"assigned_at": "2026-01-31T10:00:00Z"}, "assigned_at", datetime(2026, 1, 31, 10, tzinfo=UTC)),
        ({"assigned_at": "01/31/2026 10:00:00"}, "assigned_at", datetime(2026, 1, 31, 10, tzinfo=UTC)),
        ({"spend": "12.5"}, "outcome", 0.0),
    ],
)
def test_known_format_variants_are_normalised_not_rejected(
    change: dict[str, str], field: str, expected: object
) -> None:
    assert normalize(GOOD | change, "visit")[field] == expected


@pytest.mark.parametrize("col", ["recency", "history", "mens", "womens", "newbie", "zip_code", "channel"])
def test_a_missing_feature_is_null_and_the_unit_is_kept(col: str) -> None:
    assert normalize(GOOD | {col: "  "}, "visit")[col] is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"segment": "Test E-Mail"}, "invalid_segment"),
        ({"segment": ""}, "invalid_segment"),
        ({"visit": ""}, "missing_outcome"),
        ({"visit": "2"}, "invalid_visit"),
        ({"visit": "yes"}, "invalid_visit"),
        ({"recency": "0"}, "invalid_recency"),
        ({"recency": "99"}, "invalid_recency"),
        ({"recency": "3.5"}, "invalid_recency"),
        ({"recency": "-1"}, "invalid_recency"),
        ({"history": "-50.00"}, "invalid_history"),
        ({"history": "nan"}, "invalid_history"),
        ({"history": "lots"}, "invalid_history"),
        ({"newbie": "maybe"}, "invalid_newbie"),
        ({"zip_code": "Suburban"}, "invalid_zip_code"),  # the corrected spelling is NOT what the source uses
        ({"channel": "Fax"}, "invalid_channel"),
        ({"assigned_at": "not-a-date"}, "invalid_assigned_at"),
        ({"recency": None}, "malformed_row"),
    ],
)
def test_contract_violations_are_rejected_with_a_reason(change: dict[str, str], reason: str) -> None:
    with pytest.raises(Reject, match=f"^{reason}$"):
        normalize(GOOD | change, "visit")  # type: ignore[arg-type]


def test_the_outcome_column_is_a_choice() -> None:
    row = GOOD | {"visit": "1", "conversion": "1", "spend": "29.99"}
    assert normalize(row, "spend")["outcome"] == 29.99
    assert normalize(row, "conversion")["outcome"] == 1.0
    with pytest.raises(Reject, match="invalid_spend"):
        normalize(row | {"spend": "-1"}, "spend")


def test_ingestion_catches_exactly_the_defects_the_generator_planted(tmp_path: Path) -> None:
    m = synth.generate(tmp_path, 8000, seed=3)
    units, bad = parse(tmp_path / "synthetic.csv", "ab" * 32, "visit")
    reasons: dict[str, int] = {}
    for _, _, reason, _ in bad:
        reasons[reason] = reasons.get(reason, 0) + 1
    assert len(units) + len(bad) == m["rows_main"]  # every line is accounted for
    assert reasons["missing_outcome"] == m["late_outcome"]
    assert reasons["duplicate_row"] == m["duplicate_row"]
    assert reasons["conflicting_duplicate"] == 2 * m["conflicting_duplicate"]  # type: ignore[operator]
    assert sum(v for k, v in reasons.items() if k.startswith("invalid_")) == m["invalid"]
    assert len({u[0] for u in units}) == len(units)  # one row per unit
    assert len(units) == m["units"] - m["invalid"] - m["late_outcome"] - m["conflicting_duplicate"]  # type: ignore[operator]
    # the late file completes exactly the units whose outcome was missing
    late, late_bad = parse(tmp_path / "synthetic_late.csv", "cd" * 32, "visit")
    assert len(late) == m["late_outcome"] and not late_bad
    assert {u[0] for u in late} == {unit_id for _, unit_id, reason, _ in bad if reason == "missing_outcome"}


def test_generation_is_deterministic(tmp_path: Path) -> None:
    synth.generate(tmp_path / "a", 2000, seed=5)
    synth.generate(tmp_path / "b", 2000, seed=5)
    synth.generate(tmp_path / "c", 2000, seed=6)
    assert (tmp_path / "a" / "synthetic.csv").read_bytes() == (tmp_path / "b" / "synthetic.csv").read_bytes()
    assert (tmp_path / "a" / "synthetic.csv").read_bytes() != (tmp_path / "c" / "synthetic.csv").read_bytes()


def test_a_file_without_a_key_treats_every_row_as_a_unit(tmp_path: Path) -> None:
    # Hillstrom has no customer id: two identical rows are two customers, and must both be loaded.
    path = tmp_path / "h.csv"
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GOOD))
        writer.writeheader()
        writer.writerows([GOOD, GOOD, GOOD | {"segment": "Unknown"}])
    units, bad = parse(path, "ef" * 32, "visit")
    assert [u[0] for u in units] == ["efefefef-2", "efefefef-3"]
    assert [(line, reason) for line, _, reason, _ in bad] == [(4, "invalid_segment")]


def test_a_file_missing_a_required_column_is_refused_whole(tmp_path: Path) -> None:
    path = tmp_path / "broken.csv"
    path.write_text("recency,history\n1,2\n")
    with pytest.raises(SystemExit, match="required columns missing.*segment"):
        parse(path, "00" * 32, "visit")


def test_profile_reports_what_ingestion_will_reject(tmp_path: Path) -> None:
    synth.generate(tmp_path, 1500, seed=9)
    text = profile(tmp_path / "synthetic.csv")
    assert "`missing_outcome`" in text and "| history |" in text and "No E-Mail" in text
