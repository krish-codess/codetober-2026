# Profile of the real data (jaffle-shop seeds)

Done before the generator schema was written. First block is `tydlc profile` verbatim.

### raw_customers (100 rows)

| column | inferred type | null % | approx distinct | min | max |
|---|---|---|---|---|---|
| id | BIGINT | 0.00 | 96 | 1 | 100 |
| first_name | VARCHAR | 0.00 | 80 | Aaron | Willie |
| last_name | VARCHAR | 0.00 | 21 | A. | W. |

### raw_orders (99 rows)

| column | inferred type | null % | approx distinct | min | max |
|---|---|---|---|---|---|
| id | BIGINT | 0.00 | 96 | 1 | 99 |
| user_id | BIGINT | 0.00 | 68 | 1 | 99 |
| order_date | DATE | 0.00 | 77 | 2018-01-01 | 2018-04-09 |
| status | VARCHAR | 0.00 | 5 | completed | shipped |

### raw_payments (113 rows)

| column | inferred type | null % | approx distinct | min | max |
|---|---|---|---|---|---|
| id | BIGINT | 0.00 | 115 | 1 | 113 |
| order_id | BIGINT | 0.00 | 96 | 1 | 99 |
| payment_method | VARCHAR | 0.00 | 4 | bank_transfer | gift_card |
| amount | BIGINT | 0.00 | 30 | 0 | 3000 |


## What the profile says about the sample

| Check | Result | Consequence |
|---|---|---|
| `amount % 100 <> 0` | 0 of 113 payments | Integer division by 100 loses nothing on this sample. Hides finding 2 |
| Orders with no payment | 0 of 99 | The LEFT JOIN never produces NULL here. Hides finding 1 |
| Payments per order | 86 x one, 12 x two, 1 x three | Almost no fan-in; float re-association never shows. Hides finding 3 |
| `amount <= 0` | 3 of 113 (all zero) | Zero-value payments exist; negative ones do not, and nothing forbids them |
| Customers with no order | 38 of 100 | `number_of_orders` and lifetime value are NULL for them, not 0 |
| NULLs | none in any column | Says nothing about how NULL input is handled |
| Duplicated keys | none | |
| `last_name` | 100 of 100 are an initial and a full stop | Anonymised; a real name column would not look like this |
| `order_date` | 2018-01-01 to 2018-04-09 | 99 days; no boundary dates |
| `status` | completed 67, shipped 13, placed 13, returned 4, return_pending 2 | All five accepted values occur |
| `payment_method` | credit_card 55, bank_transfer 33, coupon 13, gift_card 12 | All four occur |

DuckDB infers BIGINT for the integer columns. The generator uses INTEGER (int32),
which is what a dbt seed gets on PostgreSQL and the narrower, more hostile choice.

Assumptions the sample would have let us make, and that the generator deliberately
breaks: every order is paid, amounts are whole dollars, amounts are non-negative, one
payment per order, names are present, dates are recent.

Ingesting these files through the boundary (`tydlc ingest`) stages 100 / 99 / 113 rows
and quarantines none. The quarantine path is exercised by `tests/test_ingest.py` with
a deliberately dirty copy: missing fields, a non-ISO date, an unknown status, a
duplicate key, an orphaned reference, a short row and a missing column.
