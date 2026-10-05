# Data dictionary

PostgreSQL 16. Source of truth: `backend/migrations/versions/` (`0001` creates everything; `0002` drops two indexes). All timestamps are
`timestamptz` stored in UTC. "Null?" means the column may be NULL and what NULL means.

## Lineage

```
feed file (immutable, on disk)
   └─ ingest_batches ─ raw_feedback (every line, byte-for-byte, append-only)
                           ├─ quarantine   (line rejected: reason + detail)
                           └─ feedback     (line accepted, normalised)
                                 ├─ embeddings   (one vector per item)
                                 ├─ annotations ─ labels   (human / simulated / gold)
                                 └─ predictions  (suggestions + queue priority from the active model)
taxonomy_versions ─ taxonomy_nodes
model_versions ─ node_metrics
jobs, api_tokens
```

Invariant (tested): every `raw_feedback` row is referenced by exactly one of `feedback` or `quarantine`.

## taxonomy_versions — one row per taxonomy change

| Column | Type | Null? | Meaning |
|---|---|---|---|
| id | integer, PK | no | Version number; the current taxonomy version is `max(id)` |
| op | text | no | `init`, `add`, `rename`, `merge`, `split`, `move`, `retire` |
| params | jsonb | no | Arguments of the operation (paths, names) |
| idempotency_key | text, unique | no | Client-supplied key; replaying it returns this row instead of applying twice |
| actor | text | no | Token name that made the change |
| labels_remapped | integer ≥ 0 | no | Labels carried over automatically by this change (count of label rows) |
| labels_flagged | integer ≥ 0 | no | Labels flagged for targeted human review (count of label rows) |
| created_at | timestamptz | no | When applied |

## taxonomy_nodes — the category tree (never deleted, only retired)

| Column | Type | Null? | Meaning |
|---|---|---|---|
| id | integer, PK | no | Stable identity; labels and models refer to this, so renames cost nothing |
| parent_id | integer → taxonomy_nodes | yes: NULL = top-level domain | Parent node |
| name | text | no | Slug, `^[a-z0-9][a-z0-9_]{0,62}$`; unique among active siblings |
| title | text | no | Display name |
| depth | smallint 1–6 | no | 1 for domains. **Derived by trigger** from the parent |
| path | text | no | `domain/entity/attribute`. **Derived by trigger**; unique among active nodes |
| created_version | integer → taxonomy_versions | no | Version that introduced the node |
| retired_version | integer → taxonomy_versions | yes: NULL = active | Version that retired it |

Triggers: path/depth derivation and cascade to descendants; cycle rejection; a node with active children cannot be retired; a child cannot be created under a retired parent.

## ingest_batches — one row per ingested feed file

| Column | Type | Null? | Meaning |
|---|---|---|---|
| id | integer, PK | no | |
| source | text | no | Default source name applied to records that carry none (`feed`, `api`) |
| file_name | text | no | Name of the feed file under `DATA_DIR/feeds` |
| file_sha256 | char(64), unique | no | SHA-256 of the file: the idempotency key of ingestion |
| n_records | integer | no | Non-blank lines in the file. Check: `n_accepted + n_quarantined = n_records` |
| n_accepted / n_quarantined | integer | no | Lines that became `feedback` / `quarantine` rows |
| n_repaired | integer | no | Accepted lines whose text was double-decoded UTF-8 and was repaired |
| n_late | integer | no | Accepted lines whose event time was > 48 h behind the source's watermark |
| started_at | timestamptz | no | |

## raw_feedback — exact bytes received (append-only)

| Column | Type | Null? | Meaning |
|---|---|---|---|
| batch_id, line_no | integer, PK | no | File and 1-based line number |
| payload | bytea | no | The line exactly as received, including invalid UTF-8 |
| received_at | timestamptz | no | |

UPDATE, DELETE and TRUNCATE raise (trigger), and the application role has no such privilege.

## quarantine — rejected lines

| Column | Type | Null? | Meaning |
|---|---|---|---|
| batch_id, line_no | PK → raw_feedback | no | The rejected line |
| reason | text (enum) | no | `bad_encoding`, `malformed_json`, `bad_field`, `missing_text`, `empty_text`, `too_long`, `bad_timestamp`, `duplicate_delivery`, `conflicting_duplicate`, `split_conflict` |
| detail | text | no | Human-readable specifics (byte offset, offending value) |
| created_at | timestamptz | no | |

## split_groups — which side of the evaluation split a group is on

| Column | Type | Null? | Meaning |
|---|---|---|---|
| group_key | text, PK | no | Leakage unit: e.g. all translations of one sentence share a key |
| split | text | no | `pool` (may be labelled and trained on) or `test` (held out, reference labels only) |

`feedback (group_key, split)` is a composite foreign key to this table, so a group cannot appear on both sides.

## feedback — accepted, normalised items

| Column | Type | Null? | Meaning |
|---|---|---|---|
| id | bigint, PK | no | |
| batch_id, line_no | → raw_feedback, unique | no | Provenance |
| source | text | no | Producing system; `^[a-z0-9][a-z0-9_-]{0,31}$` |
| external_id | text (1–200 chars) | no | Producer's id, or `sha:<hash>` when the producer sent none. Unique with `source` |
| text | text, 1–4000 **characters** | no | NFC-normalised, whitespace-collapsed, control characters removed, mojibake repaired |
| text_sha256 | char(64) | no | SHA-256 of `text` (after normalisation) |
| lang | text | yes: NULL = not supplied | ISO 639 code, lower-cased, region stripped (`en-US` → `en`). Not detected |
| created_at | timestamptz | yes: NULL = not supplied | Event time claimed by the producer |
| ingested_at | timestamptz | no | When this system stored it |
| is_late | boolean | no | Event time more than 48 h behind the newest event already seen from this source |
| group_key, split | → split_groups | no | See above |
| duplicate_of | bigint → feedback | yes: NULL = not a duplicate | First item with identical text in the pool, or the test item with identical text. Non-NULL rows are never queued or trained on |

## embeddings

| Column | Type | Null? | Meaning |
|---|---|---|---|
| feedback_id | bigint, PK → feedback | no | |
| model | text | no | Embedding model identifier including pinned revision |
| dim | smallint | no | Vector length (384) |
| vec | bytea | no | `dim` little-endian float32, L2-normalised. Check: `octet_length(vec) = dim * 4` |

## annotations — "someone has looked at this item"

| Column | Type | Null? | Meaning |
|---|---|---|---|
| feedback_id | bigint, PK → feedback | no | At most one annotation per item (last write wins) |
| annotator | text | no | Token name, `oracle` (simulated) or `gold` |
| source | text | no | `human`, `simulated` (reference label replayed by the seed), `gold` (reference label of a test item) |
| taxonomy_version | integer → taxonomy_versions | no | Taxonomy the annotator saw |
| model_version_id | integer → model_versions | yes: NULL = no model existed | Model whose suggestions were on screen |
| annotated_at | timestamptz | no | |

An annotation with zero label rows is valid: "no category applies".

## labels — the label set of an annotated item

| Column | Type | Null? | Meaning |
|---|---|---|---|
| feedback_id | bigint → annotations | no | PK with node_id |
| node_id | integer → taxonomy_nodes | no | |
| review_reason | text | yes: NULL = not flagged | Set by a taxonomy change (`split:<path>`, `new_child:<path>`, `moved:<path>`, `merged:<a>-><b>`); cleared when the item is re-annotated |

Triggers: inserting a label inserts its parent's label (recursively); deleting a label deletes its children's labels; labelling with a retired node raises. The set is therefore always ancestor-closed.

## model_versions

| Column | Type | Null? | Meaning |
|---|---|---|---|
| id | integer, PK | no | |
| status | text | no | `candidate`, `active` (at most one — partial unique index), `rejected` (failed the gate), `archived` |
| taxonomy_version | integer → taxonomy_versions | no | Taxonomy the model was trained for |
| n_labeled | integer | no | Pool items used for training |
| train_data_sha256 | char(64) | no | Hash of (taxonomy version, embedding model, item ids, label matrix) |
| code_version | text | no | Git SHA baked into the image (`dev` outside containers) |
| embed_model | text | no | Embedding model identifier |
| params | jsonb | no | Hyperparameters (`c`, `folds`, `seed`) |
| metrics | jsonb | no | Held-out results: `hf1`, `hf1_ci95`, `precision`, `recall`, `exact_match`, `consistent`, per-depth F1, `by_lang`, `calibration` (unitless, 0–1); `fit_seconds` |
| gate | jsonb | no | Promotion decision and the numbers it was based on |
| artifact | bytea | no | `.npz` of weights, calibration parameters, node ids (no pickle) |
| artifact_sha256 | char(64) | no | |
| created_at | timestamptz | no | |

## predictions — current suggestions for unlabelled pool items

A row exists only while its item is unlabelled: annotating an item deletes its row, which is what
lets the queue be a plain walk of `predictions_queue (priority DESC, feedback_id)`.

| Column | Type | Null? | Meaning |
|---|---|---|---|
| feedback_id | bigint, PK → feedback | no | One row per item: the latest scoring replaces the previous |
| model_version_id | integer → model_versions | no | Model that produced this row |
| node_ids | integer[] | no | Candidate nodes with probability ≥ 0.05, most probable first, max 12 |
| probs | real[] | no | Calibrated marginal probability per candidate (0–1); same length as `node_ids` |
| confidence | real 0–1 | no | Calibrated probability that the predicted set is exactly right |
| uncertainty | real ≥ 0 | no | Selection score (higher = more informative to label) |
| priority | real | no | Queue order; (1, 2] for the diversified head of the queue, [0, 1] for the rest |

## node_metrics — per-node, per-language confusion counts on the held-out split

| Column | Type | Null? | Meaning |
|---|---|---|---|
| model_version_id, node_id, lang | PK | no | `lang` is an ISO code or `all` |
| tp / fp / fn | integer ≥ 0 | no | Item counts. Precision = tp/(tp+fp), recall = tp/(tp+fn); rows with all three zero are not stored |

## jobs

| Column | Type | Null? | Meaning |
|---|---|---|---|
| id | integer, PK | no | |
| kind | text | no | `retrain` or `embed_score` |
| status | text | no | `queued`, `running`, `succeeded`, `failed` |
| idempotency_key | text, unique | no | Same key → same job |
| requested_by | text | no | Token name or `scheduler` |
| progress | real 0–1 | no | Updated by the worker at each stage |
| stage | text | no | Human-readable current stage |
| attempts | integer | no | Runs started (max 3) |
| error | text | yes: NULL = no error so far | Last error message |
| result | jsonb | yes: NULL until success | Outcome summary |
| requested_at | timestamptz | no | Also "not before" time when a retry is backing off |
| started_at / finished_at | timestamptz | yes | NULL until the job starts / ends |

## api_tokens

| Column | Type | Null? | Meaning |
|---|---|---|---|
| id | integer, PK | no | |
| name | text, unique | no | Appears as `annotator` / `actor` / `requested_by` |
| token_sha256 | char(64), unique | no | SHA-256 of the bearer token; the token itself is never stored |
| role | text | no | `viewer` < `annotator` < `admin` |
| created_at | timestamptz | no | |
| revoked_at | timestamptz | yes: NULL = valid | Set to revoke |

## Feed envelope (input contract)

One JSON object per line, UTF-8, ≤ 64 kB per line.

| Field | Required | Meaning |
|---|---|---|
| text | yes | The feedback |
| id | no | Producer's id; content hash is used if absent |
| source | no | Producer name; defaults to the batch's source |
| lang | no | Language tag in any common form |
| created_at | no | ISO-8601, epoch seconds, or `YYYY/MM/DD HH:MM:SS`; future timestamps are rejected |
| group | no | Leakage group; defaults to the item itself |
| eval | no | `true` puts the record in the held-out split (requires `gold`) |
| gold | no | `{"domain": str, "categories": [str]}` reference label |
