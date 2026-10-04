"""Reproductions of the findings in docs/findings.md that the seeded runner cannot gate
on, because they depend on engine state rather than on the input alone."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, Phase, given, settings

from tydlc.engine import make_engine
from tydlc.generators import datasets
from tydlc.properties import canon
from tydlc.schema import Dataset
from tydlc.subjects import Subject


@pytest.mark.xfail(strict=False, reason="DOUBLE money summed in hash-join order: the same "
                   "input gives bit-different totals on different executions (DuckDB)")
def test_same_input_gives_bit_identical_output_on_every_execution(subject: Subject) -> None:
    engine = make_engine("duckdb", subject)
    first: dict[str, dict[str, list[str]]] = {}
    drift: list[str] = []

    def record(ds: Dataset) -> None:
        out = {model: canon(rows) for model, rows in engine.execute(ds).items()}
        if first.setdefault(repr(ds), out) != out:
            drift.append(repr(ds))

    for _ in range(4):  # the same 60 datasets, four times over, on one connection
        @settings(max_examples=60, derandomize=True, database=None, deadline=None,
                  suppress_health_check=list(HealthCheck), phases=[Phase.generate])
        @given(datasets(subject.schema))
        def replay(ds: Dataset) -> None:
            record(ds)
            record({table: rows[::-1] for table, rows in ds.items()})

        replay()
    engine.close()
    assert not drift, f"{len(drift)} executions differed from the first run of the same input"
