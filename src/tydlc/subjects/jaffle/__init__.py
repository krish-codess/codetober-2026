"""dbt-labs/jaffle-shop-classic, vendored unmodified at commit fd7bfac (Apache-2.0).

The models are rendered with Jinja directly instead of through dbt-core: a property
run executes the pipeline thousands of times and a dbt invocation costs seconds.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Template

from tydlc.properties import Ctx, Property, close, same_output
from tydlc.schema import Column, Schema, Table
from tydlc.subjects import Subject

HERE = Path(__file__).parent
METHODS = ("credit_card", "coupon", "bank_transfer", "gift_card")
STATUSES = ("placed", "shipped", "completed", "return_pending", "returned")

# Constraints come from the project's own dbt tests (models/schema.yml and
# models/staging/schema.yml); column types from profiling the seeds (docs/profile.md).
# Where dbt declares nothing, columns the models compute on stay NOT NULL and with
# accepted values, so every counterexample is inside the pipeline's documented
# domain; only pass-through columns (names, order_date) are nullable.
SCHEMA: Schema = (
    Table("raw_customers", (
        Column("id", "int", unique=True),
        Column("first_name", "str", nullable=True),
        Column("last_name", "str", nullable=True),
    )),
    Table("raw_orders", (
        Column("id", "int", unique=True),
        Column("user_id", "int", references=("raw_customers", "id")),
        Column("order_date", "date", nullable=True),
        Column("status", "str", accepted=STATUSES),
    )),
    Table("raw_payments", (
        Column("id", "int", unique=True),
        Column("order_id", "int", references=("raw_orders", "id")),
        Column("payment_method", "str", accepted=METHODS),
        Column("amount", "int"),  # cents
    )),
)

_MODEL_FILES = ("staging/stg_customers", "staging/stg_orders", "staging/stg_payments",
                "customers", "orders")
MODELS = tuple(
    (Path(f).name, Template((HERE / "models" / f"{f}.sql").read_text()).render(ref=lambda n: n))
    for f in _MODEL_FILES
)


def _z(v: float | None) -> float:
    return 0 if v is None else v


def _orders_match_raw(c: Ctx) -> bool:
    return sorted(o["order_id"] for o in c.out["orders"]) == sorted(
        o["id"] for o in c.raw["raw_orders"])


def _customers_match_raw(c: Ctx) -> bool:
    return sorted(r["customer_id"] for r in c.out["customers"]) == sorted(
        r["id"] for r in c.raw["raw_customers"])


def _amount_not_null(c: Ctx) -> bool | None:
    return all(o["amount"] is not None for o in c.out["orders"]) if c.out["orders"] else None


def _method_amounts_not_null(c: Ctx) -> bool | None:
    if not c.out["orders"]:
        return None
    return all(o[f"{m}_amount"] is not None for o in c.out["orders"] for m in METHODS)


def _cents_conserved(c: Ctx) -> bool | None:
    if not c.raw["raw_payments"]:
        return None
    dollars = sum(_z(o["amount"]) for o in c.out["orders"])
    return bool(round(dollars * 100) == sum(p["amount"] for p in c.raw["raw_payments"]))


def _methods_sum_to_amount(c: Ctx) -> bool | None:
    paid = [o for o in c.out["orders"] if o["amount"] is not None]
    if not paid:
        return None
    return all(close(sum(_z(o[f"{m}_amount"]) for m in METHODS), o["amount"]) for o in paid)


def _ltv_is_sum_of_orders(c: Ctx) -> bool | None:
    if not c.out["customers"]:
        return None
    per_customer: dict[int, float] = {}
    for o in c.out["orders"]:
        per_customer[o["customer_id"]] = per_customer.get(o["customer_id"], 0) + _z(o["amount"])
    return all(close(_z(r["customer_lifetime_value"]), per_customer.get(r["customer_id"], 0))
               for r in c.out["customers"])


def _order_count_matches_raw(c: Ctx) -> bool | None:
    if not c.out["customers"]:
        return None
    counts: dict[int, int] = {}
    for o in c.raw["raw_orders"]:
        counts[o["user_id"]] = counts.get(o["user_id"], 0) + 1
    return all(_z(r["number_of_orders"]) == counts.get(r["customer_id"], 0)
               for r in c.out["customers"])


def _first_order_before_last(c: Ctx) -> bool | None:
    dated = [r for r in c.out["customers"] if r["first_order"] is not None]
    return all(r["first_order"] <= r["most_recent_order"] for r in dated) if dated else None


def _deterministic(c: Ctx) -> bool:
    return same_output(c.out, c.run(c.raw))


def _row_order_invariant(c: Ctx) -> bool | None:
    if all(len(rows) < 2 for rows in c.raw.values()):
        return None
    return same_output(c.out, c.run({t: rows[::-1] for t, rows in c.raw.items()}))


def _payment_is_local(c: Ctx) -> bool | None:
    """Metamorphic: one more $1.00 payment moves exactly one order by exactly 1.00."""
    if not c.raw["raw_orders"]:
        return None
    target = c.raw["raw_orders"][0]["id"]
    new_id = max((p["id"] for p in c.raw["raw_payments"]), default=0) + 1
    if new_id > 2**31 - 1:
        return None
    extra = {"id": new_id, "order_id": target, "payment_method": METHODS[0], "amount": 100}
    after = c.run({**c.raw, "raw_payments": [*c.raw["raw_payments"], extra]})
    before = {o["order_id"]: _z(o["amount"]) for o in c.out["orders"]}
    return all(close(_z(o["amount"]) - before[o["order_id"]], o["order_id"] == target)
               for o in after["orders"])


PROPERTIES = (
    Property("orders_one_row_per_raw_order",
             "orders has exactly the raw order ids: no fan-out, no dropped rows",
             _orders_match_raw),
    Property("customers_one_row_per_raw_customer",
             "customers has exactly the raw customer ids", _customers_match_raw),
    Property("orders_amount_not_null",
             "orders.amount is never NULL (dbt not_null test in models/schema.yml)",
             _amount_not_null),
    Property("orders_method_amounts_not_null",
             "orders.<method>_amount is never NULL (dbt not_null tests in models/schema.yml)",
             _method_amounts_not_null),
    Property("cents_conserved",
             "total order dollars x 100 equals total raw payment cents", _cents_conserved),
    Property("methods_sum_to_amount",
             "the four per-method amounts of an order add up to its amount",
             _methods_sum_to_amount),
    Property("ltv_is_sum_of_orders",
             "customer_lifetime_value equals the sum of that customer's order amounts",
             _ltv_is_sum_of_orders),
    Property("order_count_matches_raw",
             "number_of_orders equals the customer's raw order count", _order_count_matches_raw),
    Property("first_order_before_last",
             "first_order <= most_recent_order", _first_order_before_last),
    Property("deterministic", "running the pipeline twice gives the same output", _deterministic),
    Property("row_order_invariant",
             "reversing input row order does not change the output", _row_order_invariant),
    Property("payment_is_local",
             "adding a $1.00 payment raises one order's amount by 1.00 and touches no other",
             _payment_is_local),
)

_NO_PAYMENTS = ("orders LEFT JOINs order_payments, so an order with no payments gets NULL "
                "amounts; the project's own not_null tests forbid that")
_INT_DIV = ("stg_payments computes `amount / 100` on an INTEGER column; PostgreSQL truncates, "
            "so sub-dollar cents vanish (the seeds only contain multiples of 100)")
_FLOAT = ("`amount / 100` yields DOUBLE on DuckDB and float addition is not associative, "
          "so sums depend on physical row order")

SUBJECT = Subject(
    name="jaffle",
    schema=SCHEMA,
    models=MODELS,
    properties=PROPERTIES,
    known_bugs={
        "duckdb": {"orders_amount_not_null": _NO_PAYMENTS,
                   "orders_method_amounts_not_null": _NO_PAYMENTS,
                   "row_order_invariant": _FLOAT},
        "postgres": {"orders_amount_not_null": _NO_PAYMENTS,
                     "orders_method_amounts_not_null": _NO_PAYMENTS,
                     "cents_conserved": _INT_DIV},
    },
    seed_dir=HERE / "seeds",
)
