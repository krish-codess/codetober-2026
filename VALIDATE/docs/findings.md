# Findings: four real bugs in jaffle-shop-classic

The pipeline is [dbt-labs/jaffle-shop-classic](https://github.com/dbt-labs/jaffle-shop-classic)
at commit `fd7bfac`, unmodified. Its own tests pass on its own seed data. Every input
below satisfies every constraint the project declares (unique keys, valid foreign keys,
accepted values). Reproduce any of them with `tydlc run --engine <engine> --seed 28`.

| # | Property | Engine | Fails on | Minimal case |
|---|---|---|---|---|
| 1 | `orders_amount_not_null` | both | 72–87% of generated datasets | 2 rows |
| 2 | `cents_conserved` | PostgreSQL | 93–100% | 3 rows |
| 3 | `row_order_invariant` | DuckDB | 1–19% | 5 rows |
| 4 | bit-identical re-execution | DuckDB | state-dependent | see below |

The ranges are across the seeded runs recorded while building this (40 to 300 examples).

## 1. An order with no payments has NULL amounts, violating jaffle's own tests

`models/schema.yml` declares `not_null` on `orders.amount` and on all four
`orders.<method>_amount` columns. `orders.sql` builds them with
`orders LEFT JOIN order_payments`, so an order nobody has paid for yet gets NULL in all
five. The seed data never shows it: all 99 seed orders have at least one payment.

Shrunk automatically to:

```
raw_customers  {id: 0, first_name: NULL, last_name: NULL}
raw_orders     {id: 0, user_id: 0, order_date: NULL, status: "placed"}
raw_payments   (empty)
```

Fix: `coalesce(order_payments.total_amount, 0) as amount`, likewise for each method.

Discovery found the same thing independently: `not_null(orders.amount)` holds on the
seed data, is proposed as a candidate, and is falsified with the identical 2-row case.

## 2. Sub-dollar cents vanish on PostgreSQL

`stg_payments.sql`: `amount / 100 as amount`, with the comment "`amount` is currently
stored in cents, so we convert it to dollars". dbt seeds load `amount` as an integer,
and integer division truncates on PostgreSQL. A 1-cent payment becomes 0 dollars, a
$1.99 payment becomes $1. Every amount in the seed file is a multiple of 100
(`docs/profile.md`), so the project's sample data cannot reveal it.

```
raw_customers  {id: 0, ...}
raw_orders     {id: 0, user_id: 0, ...}
raw_payments   {id: 0, order_id: 0, payment_method: "credit_card", amount: 1}
```

`sum(orders.amount) * 100` is 0; `sum(raw_payments.amount)` is 1. On DuckDB `/` is
float division, so the same SQL keeps the cents and the property holds, which leads to
the next two findings.

Fix: `amount / 100.0`, or better, `(amount / 100.0)::numeric(16, 2)`.

## 3. On DuckDB, an order's total depends on the physical order of its payments

`amount / 100` is DOUBLE on DuckDB. Float addition is not associative, so
`sum(amount)` over the same payments in a different row order gives a different total.

```
raw_payments   {id: 0,  order_id: 0, amount: 1}
               {id: -1, order_id: 0, amount: 2}
               {id: 1,  order_id: 0, amount: -1}
```

0.01 + 0.02 − 0.01 forwards is not bit-equal to the same sum backwards. Three payments
on one order is the minimum: removing any one of them makes the property hold. The
difference is at the 17th significant digit, which is exactly the kind of thing that
makes a reconciliation `WHERE a.total = b.total` silently drop rows.

Not present on PostgreSQL, where the (truncated) amounts are integers.

## 4. On DuckDB, the same input gives different output on different executions

Found by accident, by this project's own test suite: the `deterministic` property
failed only when run after other tests. The same dataset, in the same row order, on
the same connection:

```
execution 15:   customers.customer_lifetime_value = -42949704.3
execution 135:  customers.customer_lifetime_value = -42949704.300000004
```

`customers.sql` sums DOUBLE amounts after `payments LEFT JOIN orders`; the join emits
rows in an order that depends on engine state, not just on the input. Same root cause
as finding 3, but nothing in the input triggers it, so no seed reproduces it and it
cannot gate CI. `tests/test_findings.py` reproduces it (60 datasets replayed four
times; some come back different) and is a non-strict xfail. The gating `deterministic`
property compares floats to three decimals.

Fix for 3 and 4: store money as `numeric`.

## What discovery added

Of 97 candidates inferred from the seed data, 60 survived 200 adversarial datasets and
37 were falsified. The falsified ones sort into three groups:

- **The real bug (finding 1), rediscovered six ways:** `not_null(orders.amount)`, the
  four `not_null(orders.<method>_amount)`, and `subset(orders.order_id,
  stg_payments.order_id)` ("every order has a payment").
- **Unstated domain assumptions worth a decision:** `non_negative(orders.amount)` and
  friends. The seed has no negative payment, the contract does not forbid one, and a
  refund makes order totals and lifetime value negative. `le(orders.coupon_amount,
  orders.amount)` fails for the same reason.
- **Coincidences of the sample:** `not_null(customers.first_name)`,
  `non_negative(orders.order_id)`, `le(stg_payments.order_id, stg_payments.payment_id)`.

Among the 60 survivors are invariants nobody wrote down, for example
`sum_eq(orders.amount, stg_payments.amount)` (money is conserved between the staging
and final models) and `row_count_eq(orders, raw_orders)` (no fan-out).
