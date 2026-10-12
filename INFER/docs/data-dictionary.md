# Data dictionary

PostgreSQL 18. The schema is `migrations/versions/0001_schema.py`; privileges are `0002_least_privilege_roles.py`.
"Uplift" is always a difference in the outcome between treated and control, in the outcome's own units (for the
default outcome `visit`, a difference in probability; the UI shows it as percentage points).

## dataset
One experiment's data, under a name.

| Column | Type | Null | Meaning |
|---|---|---|---|
| id | integer, identity | no | Primary key. |
| name | text | no | Unique. Lower-case letters, digits, underscore; at most 40 characters. |
| outcome | text | no | Which raw column is the outcome: `visit` (0/1), `conversion` (0/1) or `spend` (currency). Fixed for the dataset; a file with a different outcome is refused. |
| created_at | timestamptz | no | When the first file was ingested. |

## raw_file
One ingested file. The bytes are kept unchanged at `<data dir>/raw/<sha256>.csv`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| sha256 | text | no | Primary key: SHA-256 of the file. Ingesting the same bytes again is a no-op. |
| dataset_id | integer | no | FK to `dataset`. |
| filename | text | no | Name the file had when ingested (informational). |
| bytes | bigint | no | File size. |
| rows_total | integer | no | Data rows in the file (header excluded). |
| rows_loaded | integer | no | Rows that became units. |
| rows_quarantined | integer | no | Rows that did not. CHECK: `rows_total = rows_loaded + rows_quarantined`. |
| ingested_at | timestamptz | no | |

## unit
One experimental unit (a customer in one experiment). Features are as known **before** assignment.

| Column | Type | Null | Meaning |
|---|---|---|---|
| dataset_id, unit_id | integer, text | no | Primary key. `unit_id` is the file's `customer_id`, or `<sha8>-<line>` for a file without one. |
| raw_sha, line_no | text, integer | no | The raw file and line (1-based, header is line 1) the unit came from. Unique together. |
| treatment | boolean | no | True if the unit was sent any e-mail. |
| outcome | double precision | no | The dataset's outcome for this unit. `>= 0`. |
| assigned_at | timestamptz | yes | When the unit was assigned to an arm. NULL when the source has no timestamps (Hillstrom). |
| recency | smallint | yes | Months since last purchase, 1 to 12. |
| history | numeric(10,2) | yes | Currency spent in the past year, `>= 0`. Floored at 29.99 by the source. |
| mens, womens | boolean | yes | Bought men's / women's merchandise in the past year. |
| newbie | boolean | yes | New customer in the past twelve months. |
| zip_code | text | yes | `Surburban` (sic), `Urban` or `Rural`. |
| channel | text | yes | Purchase channel in the past year: `Web`, `Phone` or `Multichannel`. |
| true_uplift | double precision | yes | Synthetic data only: the unit's true treatment effect. Never a feature. |

NULL in a feature column means the source did not have the value. It is not an error.

## quarantine
Raw rows that were not loaded, each with the reason and the row itself.

| Column | Type | Null | Meaning |
|---|---|---|---|
| id | bigint, identity | no | Primary key. |
| raw_sha, line_no | text, integer | no | Where the row is. Unique together: a line is quarantined at most once. |
| unit_id | text | yes | The unit the row claimed to be, when it had one. |
| reason | text | no | `missing_outcome`, `invalid_<column>`, `malformed_row`, `missing_customer_id`, `duplicate_row`, `conflicting_duplicate`, `unit_already_loaded`. |
| raw | jsonb | no | The row as read, column name to string. |

## experiment
One training run over one dataset: every learner on the same rows.

| Column | Type | Null | Meaning |
|---|---|---|---|
| id | uuid | no | Primary key. |
| dataset_id | integer | no | FK to `dataset`. |
| status | text | no | `running`, `complete`, `failed`, or `retired` (complete, but rolled back). The API serves the latest `complete` one per dataset. |
| data_version | text | no | SHA-256 over the sorted hashes of the dataset's raw files at training time. If it no longer matches, the experiment is stale. |
| code_version | text | no | Git commit (or `UPLIFT_CODE_VERSION`) of the code that trained it. |
| params | jsonb | no | Learners, seed, bootstrap rounds, expected treated share, feature list. |
| split_strategy | text | yes | `temporal` or `hash`. |
| audit | jsonb | yes | Randomization and leakage checks (treated share, sample-ratio p-value, standardized mean differences, treatment AUC, missing shares), reference values (test-split effect, oracle Qini on synthetic data), and `error` when failed. |
| timings | jsonb | yes | Seconds per stage. |
| started_at, finished_at | timestamptz | no, yes | `finished_at` is set for every status except `running`. |

## model_run
One learner in one experiment.

| Column | Type | Null | Meaning |
|---|---|---|---|
| id | uuid | no | Primary key. |
| experiment_id, learner | uuid, text | no | Unique together. Learner is `random`, `response`, `s_learner`, `t_learner`, `x_learner` or `causal_forest`. |
| status | text | no | `complete` or `failed`. |
| error | text | yes | Set exactly when failed. |
| is_champion | boolean | no | True for the learner with the best validation Qini among those that estimate uplift. At most one per experiment (partial unique index). |
| artifact_file, artifact_sha256 | text | yes | Model file under `<data dir>/artifacts/` and its hash. A file that does not match the hash is never loaded. |
| fit_seconds | double precision | yes | Training time. |
| metrics | jsonb | yes | `val` and `test`: Qini with 95% bootstrap interval, AUUC, uplift in the top 10/20/30%, share of resamples beating each baseline, and on synthetic data the share of oracle Qini and PEHE. Also `calibration_slope`. |
| curves | jsonb | yes | Qini and cumulative-uplift curves on the test split, 101 `[fraction, value]` points each. |
| profiles | jsonb | yes | The top decile and the predicted-harm group on the test split: size, observed uplift with interval, distinguishing traits. |

## decile
Predicted vs observed uplift by score decile, test split. Primary key `(run_id, decile)`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| decile | smallint | no | 1 (highest predicted uplift) to 10. |
| n_treated, n_control | integer | no | Units in the decile by arm. |
| mean_predicted | double precision | no | Mean score. An uplift for uplift learners; an unscaled score for the two baselines. |
| observed_uplift, ci_low, ci_high | double precision | no | Difference in mean outcome, treated minus control, with a 95% normal interval. |
| true_uplift | double precision | yes | Synthetic data only: mean true effect in the decile. |

## segment
The model-free segment scan of an experiment's whole dataset. Primary key `(experiment_id, definition)`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| definition | text | no | One or two `feature = level` terms joined by `AND`. |
| n_treated, n_control | integer | no | At least 300 each, or the segment is not tested. |
| uplift, ci_low, ci_high | double precision | no | Observed uplift and unadjusted 95% interval. |
| p_adjusted | double precision | no | Two-sided p-value after Benjamini-Hochberg correction across all segments tested. |
| verdict | text | no | `helps` or `hurts` when `p_adjusted < 0.05` (by sign), else `unclear`. |

## score
Batch-scoring output: the champion's predicted uplift for every unit. Primary key `(run_id, unit_id)`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| run_id | uuid | no | FK to `model_run`. |
| dataset_id, unit_id | integer, text | no | FK to `unit`. |
| split | text | no | `train`, `val` or `test`: which side of the split the unit was on for this experiment. |
| uplift | double precision | no | Predicted uplift. |

## policy
A budget-constrained targeting decision. The targeted units are the first `n_targeted` test-split scores of `run_id`
ordered by `(uplift DESC, unit_id DESC)`; the export regenerates them from that definition.

| Column | Type | Null | Meaning |
|---|---|---|---|
| id | uuid | no | Primary key. |
| run_id | uuid | no | FK to `model_run`. |
| idempotency_key | text | no | Unique. A retry with the same key returns this row. |
| budget | numeric(14,2) | no | Currency available. |
| cost_per_treatment | numeric(10,4) | no | Currency per treated unit, `> 0`. |
| value_per_outcome | numeric(12,4) | no | Currency one unit of outcome is worth, `> 0`. |
| population | integer | no | Units the policy chose among (the test split). |
| n_targeted | integer | no | Units to treat. CHECK: `n_targeted <= population AND n_targeted * cost_per_treatment <= budget`. |
| min_uplift | double precision | yes | Predicted uplift of the last unit targeted; NULL when nobody is. |
| evaluation | jsonb | no | Profit per unit for the policy, treat-all and treat-none, their differences with 95% paired-bootstrap intervals, share treated, share of the treat-all effect captured; on synthetic data the true values. Or `evaluable: false` with a reason. |
| created_at | timestamptz | no | |

## Indexes

Every index and the query it serves, with captured plans, is in [evidence/explain-analyze.md](evidence/explain-analyze.md).

## Roles

| Role | May | May not |
|---|---|---|
| `uplift_owner` | Everything; runs migrations. | Used by no running service. |
| `uplift_batch_role` | SELECT, INSERT, UPDATE, DELETE on all tables. | Any DDL. |
| `uplift_api_role` | SELECT on all tables; INSERT on `policy`. | Change or delete anything, including policies. |
