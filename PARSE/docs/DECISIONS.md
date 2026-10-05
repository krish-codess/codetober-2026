# Decisions log

What was chosen, what was rejected, and why. Numbers cited here come from `reports/*.json`.
Entries are in the order the decisions were forced, not in order of importance.

## D1. Corpus: M-ABSA, pinned to a revision

**Chosen.** [M-ABSA](https://huggingface.co/datasets/Multilingual-NLP/M-ABSA) (Apache-2.0): review
sentences in 7 domains and 21 languages with `entity attribute` aspect categories. 12 languages are
used. Pinned to commit `521ab96c`.

**Why.** It is the only freely usable corpus found that is (a) real customer feedback, (b)
multilingual, and (c) labelled with categories that already form a hierarchy
(domain → entity → attribute). That gives a reference label for every item, which is what makes
label-efficiency and taxonomy-change claims measurable instead of asserted.

**Rejected.** `amazon_reviews_multi` (taken down by the publisher; star ratings only, no
categories). MASSIVE (hierarchical and multilingual, but voice-assistant commands, not feedback).
SemEval-2016 Task 5 (the original multilingual reviews; mirrors on the Hub carry English only and
the licence is restrictive).

**Known cost.** All non-English text in M-ABSA is machine translation of English originals, so
"cross-lingual" here means cross-lingual over translated text. Real Swahili feedback is noisier
than translated Swahili. See Limitations.

## D2. The feed envelope and its defects are synthetic; the text and labels are real

M-ABSA has no ids, timestamps or delivery semantics. `feed.py` wraps each real sentence in an
envelope and injects, deterministically, the defects a feedback firehose has: truncated JSON,
invalid UTF-8, double-decoded text, missing/odd language tags, missing/odd/future timestamps,
late arrivals, redelivery, double submission, edited resubmission. The purely synthetic generator
emits the same envelope, so ingestion cannot tell the sources apart. Timestamps are therefore not
real and nothing in the evaluation depends on them.

Defects are injected into the pool only. The test set is left intact so that every language
contains exactly the same sentences and per-language numbers are directly comparable.

## D3. Embeddings: `multilingual-e5-small`, int8, on ONNX Runtime — not PyTorch

**Chosen.** The 118 MB quantised ONNX build of `intfloat/multilingual-e5-small` (a 12-layer
multilingual MiniLM transformer, ~100 languages including Swahili), run with ONNX Runtime and the
`tokenizers` library. Frozen; never fine-tuned.

**Deviation from the brief's stack.** The brief lists "Transformers". The model is a transformer
and comes from that ecosystem, but the `transformers` + `torch` packages are not installed:
the serving image would grow from ~0.8 GB to ~2 GB and the development machine had 2 GB of free
disk. ONNX Runtime also gives one inference path for training and serving, so there is no
train/serve skew by construction.

**Rejected.** Fine-tuning the encoder (hours per retrain on CPU; the retrain loop here is seconds).
`paraphrase-multilingual-MiniLM-L12-v2` (50 languages, no Swahili). The fp32 ONNX file (470 MB;
could not be afforded on disk, so the cost of quantisation is **not measured** — see Limitations).
pgvector (nothing in this system does nearest-neighbour search; vectors are stored as `bytea` and
read into numpy).

**Measured.** 77–120 texts/s on an 8-core laptop CPU.

## D4. Taxonomy: domain / entity / attribute, 241 nodes, rare nodes folded

Three upstream spellings (`rooms comfort`, `LAPTOP#GENERAL`, `Seller Service#Attitude`) are
normalised by `taxonomy.canonical_path`. Categories that are not aspects (`polarity negative`) are
dropped and counted. A node with fewer than 8 pool examples is folded into its parent: items keep
the parent label. The result is committed as `taxonomy_v1.json` and is the seed of version 1.

- The domain is level 1 even though a sentence such as "It was great" cannot reveal its domain.
  That ambiguity is real in a mixed feed and caps level-1 F1 at ~0.78 here.
- Every item carries its domain node, including the 18% of sentences with no aspect.
- Support counts used to fold nodes come from the pool only, never the test split.

## D5. Classifier: one logistic regression per node, conditioned on its parent

`P(node) = P(node | parent) · P(parent)`, each factor a logistic regression on the embedding,
trained only on examples where the parent is positive.

**Why.** (1) Consistency by construction: a product cannot exceed its factor. (2) Each classifier
sees a small, relevant contrast set (siblings), which is what makes a few hundred labels usable.
(3) A retrain is 1–60 s on CPU, so the loop "label 50 → retrain → requeue" is interactive.
(4) Weights are 241 × 384 floats: the artifact lives in a database row.

**Measured against the flat alternative** (same embeddings, every node trained independently, no
enforcement): the flat model emits a label without its parent on **10.7%** of test items and
scores 0.605 hF1; the hierarchical model emits **0** such sets and scores 0.621.

**Rejected.** Fine-tuned encoder with a hierarchical softmax head (accuracy would likely be
higher; retraining cost and the disk constraint ruled it out). Gradient-boosted trees on embeddings
(slower, no better in a quick check, and not calibrated).

Consistency is enforced in three independent places, so that no single bug can break it:
the product in `marginals`, the parent check in `decode`, and the database trigger on `labels`.

## D6. Hyperparameters and thresholds never see the test split

`C` is chosen on a 2,000-item hold-out of the pool (`reports/hyperparams.json`; `C=30` wins at all
three budgets). The decision threshold and all calibration parameters are fitted on out-of-fold
predictions over the labelled items. An earlier exploratory run tuned `C` against the test set;
that run was discarded and the harness was written to make it impossible.

## D7. Split by group, using the corpus's own test split

The 12 language versions of one sentence share a `group_key`; the corpus's official train/dev/test
split is aligned across languages (verified: 0 category mismatches over 14,776 lines). Pool =
train + dev, test = test. A composite foreign key makes it impossible to store a group on both
sides. Random splitting would have put translations of test sentences in the pool.

Additionally, 55 pool sentences are character-for-character identical to a test sentence
(short generic reviews). They are kept but pointed at the test row (`duplicate_of`) and are never
queued or trained on.

## D8. Pool = one language per sentence; test = every language

Each pool sentence is assigned a single language by a seeded draw from a skewed mix (34% English …
1.5% Swahili, 1.5% Thai). A real feed does not contain every message in twelve languages, and
keeping all of them would have handed the model twelve labelled translations per label.
The test set keeps all languages, which makes language the only thing that differs between
per-language scores.

## D9. Active learning: the principled score failed; the default is a hedge

**First design.** Rank by the entropy of the tree-factorised joint,
`Σ_j P(parent_j) · H(P(j | parent_j))`.

**Result.** Far worse than random: 0.40 vs 0.54 hF1 at 1,000 labels.

**Diagnosis.** A sum over nodes grows with the number of children. One domain (`phone`, the widest
subtree) received **73%** of all picks against a 14% share of the feed; no-aspect sentences fell
from 18% to 4%. The labelled set stopped resembling the data, and micro-averaged accuracy
collapsed even though rare nodes were found faster (macro-F1 went up).

**Fix.** `least_confident = max_j P(parent_j) · (1 − |2·P(j|parent_j) − 1|)` — a max, so breadth
earns nothing. The service default (`mixed`) fills half of each batch with that score, one item per
k-means cluster so a batch is not near-duplicates, and half uniformly at random so the labelled
set stays representative and calibration stays honest.

**What it buys.** Not much, and the README says so: see the label-efficiency table. The failed
strategy is still run in the experiment, drawn on the chart and pinned by a unit test
(`test_summed_entropy_rewards_wide_branches_but_uncertainty_does_not`).

## D10. Calibration at three granularities

Per-depth Platt scaling of node probabilities, then the parent bound is re-imposed. Whole-set
confidence ("is every label right?") is Platt-scaled separately.

Whole-set confidence is well calibrated (ECE 0.02 vs 0.42 raw) but on a 241-node multi-label
problem it is almost never high, so a threshold on it routes nothing. The routing tables an
operator can act on are therefore reported per label and per top-level domain as well.

## D11. Promotion gate uses the test split

A candidate is promoted only if its test hF1 is within `GATE_MAX_DROP` (0.02) of the active
model's. Reusing the test split for a go/no-go decision at every retrain slowly leaks it; with a
one-bit decision per retrain this was judged acceptable. A separate gate split is the upgrade.
When the taxonomy version changed, scores are not comparable and the gate passes with that reason
recorded.

## D12. Taxonomy changes are operations on labels, not a restart

| Operation | Automatic | Flagged for review |
|---|---|---|
| rename | everything (labels reference ids) | nothing |
| merge A into B | A's labels move to B | A's old parent, only where nothing else justifies it |
| split A / add child under A | labels on A stay valid | items whose most specific label is A |
| move | new ancestors are added | old parent, only where nothing else justifies it |
| retire | items fall back to the parent | nothing |

Flags apply to the reference (test) labels too; only an admin can resolve those, and only while
flagged. Each operation is one transaction inside a savepoint, one `taxonomy_versions` row, and
idempotent by key. A retrain is queued automatically.

## D13. Database choices

- **Raw payload as `bytea`.** A malformed or mis-encoded line cannot be stored as `jsonb` or
  `text`; quarantine is only useful if it shows exactly what arrived.
- **Model artifact in the row.** Removes a shared volume between worker and API, and makes
  `pg_dump` a complete backup. Would be revisited above a few MB per model.
- **Triggers for invariants.** Label closure, path derivation, append-only raw data. The rule
  "do not leave integrity to application code" is taken literally; the tests insert with raw SQL to
  prove it.
- **Bulk writes via `unnest`.** One round trip per 5,000 rows; `executemany` paid one per row.
- **Duplicate resolution in memory during ingest.** The set-based `UPDATE … FROM (aggregate)` ran
  for 14+ minutes on 56k freshly inserted rows because the planner had no statistics and chose a
  nested loop; the application role cannot `ANALYZE`. Found only on real data.
- **Two roles.** `parse_owner` runs migrations; `parse_app` (API, worker, seed) has DML only and
  no `UPDATE/DELETE/TRUNCATE` on `raw_feedback`.

## D14. API choices

- Bearer tokens stored as SHA-256, three ordered roles, checked by a dependency on every route.
- One error shape; constraint violations map to 409, a dead database to 503 with `Retry-After`.
- Keyset cursors everywhere. The queue's sort key is `(priority DESC, id)`.
- `PUT` for annotations (naturally idempotent); `Idempotency-Key` required for taxonomy changes
  and job creation; ingestion is idempotent by content hash.
- The transaction commits before the response is sent (`Depends(..., scope="function")`), so a
  client never reads stale data after its own write and a failed commit is never reported as success.
- `/metrics` is served by the API but not proxied by nginx.

## D15. Frontend choices

- TanStack Query: nothing refetches on focus or remount; each query states its own `staleTime`;
  mutations invalidate named keys. The queue is consumed from a local buffer and refetched only
  when it runs low.
- No router, no component library, no chart library: three views, hand-written CSS with light and
  dark tokens, one SVG chart.
- Biome instead of ESLint + Prettier (one dependency).
- The annotator's selection is derived state, not synced by an effect: an effect left one frame in
  which a new item showed nothing selected and Enter would have saved an empty label set.

## D16. Deliberately not built

| Not built | Why | When to build it |
|---|---|---|
| Language detection | 3% of the pool has no language tag; classification does not need it (the encoder is multilingual), only reporting does. They are reported as `und` | When untagged volume matters for per-language reporting |
| Sentiment / "is this a complaint" | M-ABSA has polarity, but the brief's requirements are all about the taxonomy | As a separate binary head on the same embeddings |
| Multi-annotator agreement, item leasing | One annotation per item, last write wins | Before more than a couple of people label concurrently |
| Encoder fine-tuning | See D3, D5 | When a GPU is available and the hF1 ceiling (0.62) is the bottleneck |
| A separate gate split | See D11 | Before the gate decides anything expensive |
| Per-tenant data | Roles exist, tenants do not | When there is a second customer |
| Cloud deployment | Target is "containerised services"; verified locally, see docs/OPERATIONS.md | When there is somewhere to deploy to |
| OpenAPI-generated TypeScript client | 12 hand-written interfaces | When the API surface doubles |
