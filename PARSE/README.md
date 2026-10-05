# What are they actually complaining about

Sorts multilingual customer feedback into a 241-node, three-level taxonomy
(domain → entity → attribute), with a labelling loop that decides what a person should label next
and a taxonomy that can change without throwing labels away.

Codetober day 21 · PARSE · Python, scikit-learn, ONNX Runtime, PostgreSQL, FastAPI, React, Docker

![Labelling on a phone](docs/screenshots/label-phone.png)

## What was found — read this first

Everything below is measured on real review text in 12 languages
([M-ABSA](https://huggingface.co/datasets/Multilingual-NLP/M-ABSA)), held-out split of 45,528
items, numbers from `reports/*.json`. "hF1" is hierarchical micro-F1: a prediction gets credit for
each correct node on the path, so the right parent of a wrong leaf is partly right.

| The brief asked for | Result | Evidence |
|---|---|---|
| A child label implies its parent | **Holds, by construction and by database trigger.** 0 of 45,528 predicted sets violate it; the same model without the hierarchy violates it on 10.7% | [baselines](reports/baselines.json), tests |
| Active learning reaches target accuracy with far fewer labels | **Not demonstrated. Active learning did not beat random sampling here.** The first, textbook strategy was much *worse* than random; the corrected one ties it | [label efficiency](#label-efficiency-the-honest-version) |
| Reaches accuracy after a few hundred labels | **Partly.** 391 labels reach 80% of what labelling all 10,733 gives; 750 reach 85%; 1,516 reach 90%; 95% is not reached within 3,000. The efficiency comes from the pretrained multilingual embeddings, not from the sampling strategy | same |
| Transfers across languages with minimal labels | **Yes, with a visible cost.** English-only training scores 0.38–0.49 on other languages (0.68 on English). Adding 25 Swahili labels to an English model gives 0.42; those 25 alone give 0.20 | [cross-lingual](#cross-lingual-transfer) |
| Taxonomy changes without discarding labels | **Yes.** Splitting a node required re-examining 40 of 1,500 labelled items and gave exactly the result of relabelling all 1,500 | [taxonomy change](#taxonomy-change) |
| Performance per node, exposing weak branches | **Yes**, per node and per language, with intervals | UI, `GET /api/v1/metrics/nodes` |
| Calibrated confidence for routing | **Yes at label and domain level** (calibration error 0.013 and 0.046). Whole-label-set confidence is calibrated but never high enough to route on | [calibration](#calibration-and-routing) |

Absolute accuracy is modest: **hF1 0.621** with every pool item labelled (domain level 0.78, entity
0.60, attribute 0.48; the entire label set is exactly right for 21% of items). This is a frozen
small encoder with linear heads on a 241-way multi-label problem. It is useful for routing by
domain and for suggesting labels to a person; it is not an unattended classifier for the leaves.

## Architecture

```mermaid
flowchart LR
    subgraph inputs[Inputs · immutable]
        HF[(M-ABSA @ pinned revision)]
        GEN[synthetic generator]
        POST[POST /feedback NDJSON]
    end
    HF --> FEED[feed file<br/>envelope + injected defects]
    GEN --> FEED
    FEED --> ING
    POST --> ING[ingest<br/>validate at boundary]
    ING -->|every line, byte for byte| RAW[(raw_feedback<br/>append-only)]
    ING -->|rejected + reason| Q[(quarantine)]
    ING -->|accepted| FB[(feedback)]
    FB --> EMB[embed<br/>multilingual-e5-small int8, ONNX Runtime]
    EMB --> VEC[(embeddings)]

    subgraph loop[Active-learning loop]
        VEC --> TRAIN[train<br/>one classifier per node, P of node given parent]
        LAB[(annotations + labels<br/>ancestor closure by trigger)] --> TRAIN
        TRAIN --> EVAL[evaluate on held-out split<br/>calibrate · per node · per language]
        EVAL --> GATE{regression gate}
        GATE -->|pass| MV[(model_versions<br/>artifact + data hash + code version)]
        MV --> SCORE[score pool<br/>suggestions + uncertainty]
        SCORE --> PRED[(predictions<br/>queue priority)]
    end

    PRED --> API
    API[FastAPI<br/>auth · cursors · idempotency] <--> WEB[React UI<br/>label · performance · taxonomy]
    WEB -->|PUT annotation| API --> LAB
    API -->|taxonomy change| TAX[(taxonomy_versions + nodes)]
    TAX -->|remap automatically, flag only affected items| LAB
    WORKER[worker<br/>jobs · scheduled retraining] --> TRAIN
    API -->|queue job| WORKER
    API -->|POST /classify| OUT[consistent label set<br/>+ calibrated confidence + route]
```

Six containers: `db`, `migrate` (owner credential, exits), `seed` (exits), `api`, `worker`, `web`
(nginx; only `/api/` is proxied).

## Run it

Requirements: Docker with Compose v2. Nothing else.

```bash
cp .env.example .env
docker compose up --build -d --wait
scripts/smoke.sh                     # verifies the stack from outside
```

Open <http://127.0.0.1:8141> and sign in with a token from `.env`
(`ANNOTATOR_TOKEN` to label, `ADMIN_TOKEN` for taxonomy changes and retraining).

- The default seed downloads the real corpus and embeds 56k texts on CPU: **about 15 minutes** the
  first time, then cached. For a 40-second boot with generated data:
  `SEED_SOURCE=synthetic EMBED_BACKEND=hash docker compose up --build -d --wait`.
- If the corpus cannot be downloaded the seed retries with backoff, then falls back to generated
  data and says so in its log (`docker compose logs seed`).
- Ports are in `.env` (`WEB_PORT` 8141, `API_PORT` 8041, `DB_PORT` 5441), bound to loopback.
- API docs: <http://127.0.0.1:8041/api/v1/docs>, or [docs/API.md](docs/API.md).

Verified from a clean state: the images build from a clean context, and an isolated stack with
empty volumes boots and passes the smoke test (`docs/evidence/`).

### Develop without Docker

```bash
docker compose up -d db                                   # PostgreSQL only
cd backend && python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,experiments]"
export DATABASE_URL=postgresql+psycopg://parse_owner:change-me-owner@127.0.0.1:5441/parse
export APP_DB_USER=parse_app APP_DB_PASSWORD=change-me-app DATA_DIR=../data
alembic upgrade head
python -m parse_app.pipeline seed                         # or: feed | ingest | embed | simulate N | train
uvicorn parse_app.api:app --port 8041 &  python -m parse_app.worker &
cd ../frontend && npm ci && npm run dev                   # http://localhost:5173, proxies /api
```

Use `127.0.0.1`, not `localhost`, in database URLs on Windows (IPv6 resolution stalls each connection).

### Tests

```bash
cd backend
ruff check . && ruff format --check . && mypy             # lint, format, strict types
TEST_DATABASE_URL=postgresql+psycopg://parse_owner:change-me-owner@127.0.0.1:5441/parse_test pytest
cd ../frontend && npm run lint && npm run typecheck && npm test
PW_CHANNEL=msedge npm run e2e                             # against a running stack
```

| Suite | Count | What it covers |
|---|---|---|
| Unit | 51 | taxonomy normalisation, tree closure, classifier consistency for arbitrary weights, calibration, selection strategies, metrics with known values, boundary validation of every defect, download retry/backoff |
| Integration + data (real PostgreSQL) | 25 | row counts, uniqueness, split isolation, append-only raw data, label closure by trigger, idempotent ingest, promotion gate, worker retry/failure injection, all taxonomy operations |
| API contract (real PostgreSQL) | 57 | every endpoint: success, validation failure, 401/403, malformed input; pagination; idempotency; degraded modes |
| Model | (in the above) | training smoke test, inference contract + artifact round trip, evaluation regression gate against `tests/baseline.json` |
| Frontend components | 13 | loading / empty / error / stale / partial states, keyboard flow, optimistic save and rollback |
| End-to-end (Playwright, 390 px wide) | 1 | sign in → label with the keyboard → next item → performance → no admin controls for an annotator |

The integration tests need `TEST_DATABASE_URL` (create the database once:
`docker compose exec db createdb -U parse_owner parse_test`); without it they are skipped.

## Label efficiency, the honest version

![Label efficiency](docs/figures/label_efficiency.png)

Simulation on the real pool (10,733 items), 3 seeds, same random first 100 for every strategy,
batches of 50–250, evaluated on the held-out split.

| Labels | Random | Mixed (default) | Least confident | Summed entropy |
|---|---|---|---|---|
| 300 | 0.474 | 0.484 | 0.472 | 0.365 |
| 500 | 0.508 | 0.507 | 0.499 | 0.372 |
| 1,000 | 0.542 | 0.539 | 0.539 | 0.396 |
| 2,000 | 0.569 | 0.566 | 0.571 | 0.486 |
| 3,000 | 0.579 | 0.583 | 0.588 | 0.540 |
| all 10,733 | 0.621 (95% CI 0.614–0.628) | | | |

| Labels needed to reach | Random | Mixed | Least confident | Summed entropy |
|---|---|---|---|---|
| 80% of full (0.497) | 391 | 419 | 468 | 2,201 |
| 85% of full (0.528) | 750 | 748 | 799 | 2,774 |
| 90% of full (0.559) | 1,516 | 1,644 | 1,564 | not within 3,000 |

What happened:

1. **The first strategy failed badly.** Ranking by the entropy of the tree-factorised joint is the
   principled choice, and it scored 0.40 at 1,000 labels against 0.54 for random. The sum over
   nodes grows with the number of children, so one domain with a wide subtree received 73% of all
   picks while being 14% of the feed. The labelled set stopped looking like the data.
2. **The fix removes the damage but adds no gain.** Taking the max over nodes instead of the sum,
   and mixing in random picks, brings the curve back to random's — within one standard deviation
   at every budget. Least-confident is slightly ahead at 3,000 labels (0.588 vs 0.579) and finds
   rare nodes better (macro-F1 0.21 vs 0.19); that is the whole advantage.
3. **So the label efficiency here is the encoder's.** A model that has never seen a label already
   places sentences well enough that a few hundred random labels reach 80% of full supervision.

The failed strategy is still in the code, in the experiment and on the chart, and a unit test pins
the mechanism. The service default is `mixed`: no worse than random, and it keeps the option open
for a setting where uncertainty helps more (a fine-tuned encoder, a more skewed feed).

## Baselines

Full pool, same test split.

| Model | hF1 (95% CI) | Whole set right | Consistent sets | Swahili hF1 |
|---|---|---|---|---|
| Most frequent domain only | 0.067 | 0.1% | 100% | — |
| Character n-gram TF-IDF, one classifier per node | 0.252 (0.246–0.260) | 4.5% | 98.6% | 0.150 |
| Embeddings, flat (no hierarchy) | 0.605 (0.599–0.612) | 16.7% | **89.3%** | 0.503 |
| **Embeddings, hierarchical** | **0.621 (0.614–0.628)** | **21.3%** | **100%** | **0.519** |

With 500 labels: TF-IDF 0.009, flat 0.486, hierarchical 0.496. Hyperparameters were chosen on a
hold-out of the pool, never on the test split ([reports/hyperparams.json](reports/hyperparams.json)).

## Cross-lingual transfer

![Languages](docs/figures/languages.png)

The test set is the same 3,794 sentences in every language, so language is the only variable.

| Trained on | en | de | es | zh | hi | th | sw |
|---|---|---|---|---|---|---|---|
| Full mixed pool (34% English … 1.5% Swahili) | 0.696 | 0.646 | 0.646 | 0.635 | 0.613 | 0.600 | 0.519 |
| 3,655 English items only (zero-shot elsewhere) | 0.681 | 0.491 | 0.416 | 0.354 | 0.475 | 0.476 | 0.376 |
| 3,655 items in the natural language mix | 0.662 | 0.608 | 0.611 | 0.601 | 0.579 | 0.568 | 0.484 |

Few-shot in the low-resource tail (target-language hF1):

| Target-language labels | Swahili: English + k | Swahili: k alone | Thai: English + k | Thai: k alone |
|---|---|---|---|---|
| 0 | 0.376 | — | 0.476 | — |
| 25 | 0.423 | 0.197 | 0.512 | 0.214 |
| 100 | 0.463 | 0.352 | 0.558 | 0.430 |
| all available (172 / 160) | 0.458 | 0.390 | 0.560 | 0.467 |

Transfer is real (English labels are worth far more than nothing) and lossy (zero-shot costs
0.2–0.3 hF1). A few dozen target-language labels recover a large part of the gap.

## Taxonomy change

`hotel/rooms` was collapsed to a single node, 1,500 items were labelled under that taxonomy, then
the node was split into its six real children.

| Policy | Items relabelled | hF1 | F1 on the new children |
|---|---|---|---|
| Keep labels, relabel nothing | 0 | 0.555 | 0.000 |
| **Targeted: only items whose most specific label was the split node** | **40** | **0.556** | **0.293** |
| Relabel everything | 1,500 | 0.556 | 0.293 |
| Discard labels, spend 40 on new items | 40 | 0.271 | 0.004 |

The targeted relabel produces a label matrix identical to the full relabel (asserted in the
experiment). In the running system this is `POST /api/v1/taxonomy/changes`: one transaction,
idempotent, reports how many labels were carried over and how many were queued in the
"Needs review" queue; the same applies to rename, merge, move, add and retire
([decisions D12](docs/DECISIONS.md)).

## Calibration and routing

![Calibration](docs/figures/calibration.png)

Model trained on the full pool; calibration fitted on out-of-fold predictions only.

| Threshold | Route by domain: items covered | …routed correctly | Accept labels: correct | …share of true labels |
|---|---|---|---|---|
| ≥ 0.70 | 67.3% | 91.7% | 87.6% | 32.1% |
| ≥ 0.80 | 58.6% | 93.9% | 91.7% | 25.2% |
| ≥ 0.90 | 46.2% | 96.5% | 95.6% | 17.2% |
| ≥ 0.95 | 36.0% | 97.9% | 97.6% | 11.9% |

Whole-label-set confidence: calibration error 0.024 (0.419 before calibration), but only 0.1% of
items exceed 0.8, so it is reported and not used for routing. `POST /classify` returns the
per-label probabilities and the set confidence; the routing rule is the caller's.

## Per-node performance

The Performance view lists every node with support, precision, recall, F1 and 95% Wilson
intervals, for all languages or one, and filters to weak branches (F1 < 0.4 with ≥ 20 examples).
Weakest well-supported nodes with the full pool: `laptop/hardware` (0.05),
`food/shipment/quality` (0.08), `coursera/course/comprehensiveness` (0.09),
`sight/teaching_setup` (0.10), `food/food/style_options` (0.10).

![Performance](docs/screenshots/performance.png)

More screenshots: [routing tables](docs/screenshots/performance-routing.png),
[weak nodes](docs/screenshots/performance-weak-nodes.png),
[taxonomy admin](docs/screenshots/taxonomy-admin.png),
[labelling, desktop dark](docs/screenshots/label-desktop-dark.png).

## Reproduce the numbers

```bash
docker compose up -d db
cd backend && pip install -e ".[dev,experiments]"
export DATABASE_URL=postgresql+psycopg://parse_owner:change-me-owner@127.0.0.1:5441/parse DATA_DIR=../data
alembic upgrade head
python -m parse_app.pipeline seed          # downloads the pinned corpus + model, fills the embedding cache
python -m parse_app.experiments all        # ~30 min on 8 cores -> reports/*.json
python -m parse_app.figures                # -> docs/figures/
```

Seeds are fixed, upstream revisions are pinned, and three different code versions trained on the
same 300 labels produced the identical score (0.4848).

## Documentation

| | |
|---|---|
| [docs/DECISIONS.md](docs/DECISIONS.md) | what was chosen, rejected, and deliberately not built |
| [docs/DATA_PROFILE.md](docs/DATA_PROFILE.md) | what the real data looked like, and every defect and its handling |
| [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md) | every table and column, units and nullability |
| [docs/API.md](docs/API.md) | API reference, generated from the code (CI checks it is current) |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | deploy, rollback, backup/restore, failure behaviour, scale ceilings |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | budgets vs measurements |
| [docs/explain/](docs/explain/README.md) | `EXPLAIN ANALYZE` for every index |
| [docs/evidence/](docs/evidence/) | transcripts: smoke test, deploy/rollback/restore drill |

## Limitations

- **Active learning adds nothing measurable over random sampling** in this configuration. See above.
- **Accuracy is modest** (hF1 0.62 at best; 0.48 at attribute level). 205 of 241 nodes are "weak"
  with 300 labels. The system is a labelling accelerator and a domain router, not a replacement
  for review at leaf level.
- **Non-English text is machine-translated** from English originals. Cross-lingual numbers are
  therefore optimistic relative to native feedback, and the per-language comparison is cleaner than
  reality would allow.
- **The envelope (ids, timestamps, delivery defects) is synthetic.** Defect handling is tested
  thoroughly, but the defect rates are invented, and nothing temporal (drift, seasonality) is
  evaluated because the corpus has no real time axis.
- **The cost of int8 quantisation was not measured**: the fp32 model did not fit on the
  development machine's disk.
- **"Human" labels in every experiment and in the seed are the corpus's reference labels**
  replayed by a simulated annotator. Real annotators disagree; there is no agreement measurement,
  one annotation per item, last write wins, no item leasing.
- **The held-out split is reused by the promotion gate** at every retrain (one bit per retrain).
- **No language detection**: 3% of items arrive without a language tag and are reported as `und`.
- **A taxonomy split leaves the new children without reference labels** until an admin works
  through the flagged test items; until then their held-out support is zero and the UI says so.
- **Deployed locally only.** Deploy, rollback and restore were executed against a local Docker
  host; there is no cloud environment, TLS, or secret manager. Tokens are static bearer tokens.
- **CI has not run on GitHub.** The workflow is in `.github/workflows/ci.yml` and every step in it
  was run locally, but this folder now lives inside a monorepo, and GitHub only reads workflows from
  the repository root: the file has to be moved or referenced there (with `PARSE/` path prefixes)
  to take effect.
- Single worker assumed; metrics are per process; the dedupe maps at ingest are in memory.
  Each has a `ponytail:` comment naming the ceiling and the upgrade.
