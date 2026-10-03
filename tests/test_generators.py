"""The generator's contract: always referentially valid, reliably hostile, reproducible."""

from __future__ import annotations

from collections.abc import Callable
from random import Random

from hypothesis import HealthCheck, Phase, find, given, seed, settings

from tydlc.generators import datasets
from tydlc.schema import Dataset
from tydlc.subjects.jaffle import SCHEMA

FAST = settings(max_examples=150, database=None, deadline=None,
                suppress_health_check=list(HealthCheck), phases=[Phase.generate, Phase.shrink])


@FAST
@given(datasets(SCHEMA))
def test_every_dataset_satisfies_the_schema(ds: Dataset) -> None:
    for table in SCHEMA:
        rows = ds[table.name]
        for col in table.columns:
            values = [r[col.name] for r in rows]
            present = [v for v in values if v is not None]
            assert col.nullable or len(present) == len(values), f"{table.name}.{col.name} null"
            if col.unique:
                assert len(set(values)) == len(values), f"{table.name}.{col.name} duplicated"
            if col.accepted:
                assert set(present) <= set(col.accepted)
            if col.references:
                parent, parent_col = col.references
                assert set(present) <= {r[parent_col] for r in ds[parent]}, "dangling reference"
            if col.type == "int":
                assert all(-(2**31) <= v < 2**31 for v in present)


def _can_generate(predicate: Callable[[Dataset], object]) -> Dataset:
    return find(datasets(SCHEMA), predicate, random=Random(0), settings=FAST)


def test_generates_the_shapes_clean_samples_never_contain() -> None:
    """Each of these is absent from the jaffle seeds and is what exposes a bug."""
    _can_generate(lambda ds: any(o["id"] not in {p["order_id"] for p in ds["raw_payments"]}
                                 for o in ds["raw_orders"]))  # order with no payment
    _can_generate(lambda ds: any(p["amount"] % 100 for p in ds["raw_payments"]))  # odd cents
    _can_generate(lambda ds: any(p["amount"] < 0 for p in ds["raw_payments"]))  # refund
    _can_generate(lambda ds: any(c["first_name"] is None for c in ds["raw_customers"]))
    _can_generate(lambda ds: any(c["first_name"] == "" for c in ds["raw_customers"]))
    _can_generate(lambda ds: len({p["order_id"] for p in ds["raw_payments"]})
                  < len(ds["raw_payments"]))  # several payments on one order
    _can_generate(lambda ds: ds["raw_customers"] and not ds["raw_orders"])


def test_shrinks_to_the_smallest_dataset() -> None:
    minimal = _can_generate(lambda ds: len(ds["raw_payments"]) >= 2)
    assert [len(minimal[t.name]) for t in SCHEMA] == [1, 1, 2]


def test_same_seed_same_data() -> None:
    def generate() -> list[Dataset]:
        seen: list[Dataset] = []

        @seed(7)
        @settings(max_examples=25, database=None, deadline=None, phases=[Phase.generate],
                  suppress_health_check=list(HealthCheck))
        @given(datasets(SCHEMA))
        def collect(ds: Dataset) -> None:
            seen.append(ds)

        collect()
        return seen

    assert generate() == generate()
