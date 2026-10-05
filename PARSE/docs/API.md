# API reference — What are they actually complaining about v1.0.0

> Generated from the code by `python -m parse_app.apidoc`. Do not edit by hand; CI fails if it drifts.
> Interactive version: `/api/v1/docs` on a running stack. Machine-readable: `/api/v1/openapi.json`.

Hierarchical multilingual feedback classification with an active-learning labelling loop.

All endpoints except `/health` need `Authorization: Bearer <token>`. Roles: viewer < annotator < admin.
Errors always look like `{"error": {code, message, details, request_id}}`.
List endpoints are cursor-paginated: pass `next_cursor` back as `cursor`.

## `GET /api/v1/health`

Exercises each dependency: a query against the schema, deserialising the active model,
and a real embedding call. `down` (503) only when the database is unreachable.

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | HealthOut |
| 503 | Service Unavailable | HealthOut |

## `GET /api/v1/me`

Me

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | Principal |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `GET /api/v1/stats`

Stats

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | StatsOut |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `GET /api/v1/taxonomy`

Get Taxonomy

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | TaxonomyOut |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `POST /api/v1/taxonomy/changes`

Apply one taxonomy change atomically. Existing labels are remapped or kept; only the items
whose answer could differ are flagged for review. A retrain is queued automatically.

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `Idempotency-Key` | header | string | yes | minLength=8, maxLength=200, pattern='^[\\x21-\\x7e]+$'; Client-chosen key; repeating a request with the same key is a no-op. |
| `authorization` | header | string | null | no |  |

Request body (`application/json`): **AddOp | SplitOp | RenameOp | MergeOp | MoveOp | RetireOp**

| Status | Meaning | Body |
|---|---|---|
| 201 | Successful Response | ChangeOut |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `GET /api/v1/taxonomy/changes`

List Changes

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `limit` | query | integer | no | minimum=1, maximum=100, default=20 |
| `cursor` | query | string | null | no |  |
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | ChangesPage |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `GET /api/v1/queue`

What to label next. `uncertain`: unlabelled items ordered by diversified model uncertainty.
`review`: items a taxonomy change flagged, with fresh suggestions from the active model.

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `mode` | query | "uncertain" | "review" | no | default='uncertain' |
| `limit` | query | integer | no | minimum=1, maximum=100, default=20 |
| `cursor` | query | string | null | no |  |
| `lang` | query | string | null | no |  |
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | QueuePage |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `PUT /api/v1/items/{item_id}/annotation`

Replace an item's label set (idempotent). Clears any review flag on the item.

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `item_id` | path | integer | yes |  |
| `authorization` | header | string | null | no |  |

Request body (`application/json`): **AnnotationIn**

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | AnnotationOut |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `POST /api/v1/classify`

Classify

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `authorization` | header | string | null | no |  |

Request body (`application/json`): **ClassifyIn**

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | ClassifyOut |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `GET /api/v1/models`

List Models

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `limit` | query | integer | no | minimum=1, maximum=100, default=20 |
| `cursor` | query | string | null | no |  |
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | ModelsPage |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `GET /api/v1/metrics/nodes`

Per-node precision/recall on the held-out split, with 95% Wilson intervals, for one language or all.

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `model_version` | query | integer | null | no |  |
| `lang` | query | string | no | pattern='^([a-z]{2,3}|all)$', default='all' |
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | NodeMetricsOut |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `GET /api/v1/metrics/efficiency`

Label-efficiency curve: held-out hF1 against number of labelled items.

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | EfficiencyOut |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `POST /api/v1/jobs`

Queue a retrain. Safe to retry: the same Idempotency-Key returns the same job.

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `Idempotency-Key` | header | string | yes | minLength=8, maxLength=200, pattern='^[\\x21-\\x7e]+$'; Client-chosen key; repeating a request with the same key is a no-op. |
| `authorization` | header | string | null | no |  |

Request body (`application/json`): **JobIn**

| Status | Meaning | Body |
|---|---|---|
| 202 | Successful Response | JobOut |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `GET /api/v1/jobs`

List Jobs

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `limit` | query | integer | no | minimum=1, maximum=100, default=10 |
| `cursor` | query | string | null | no |  |
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | JobsPage |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `GET /api/v1/jobs/{job_id}`

Get Job

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `job_id` | path | integer | yes |  |
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | JobOut |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## `POST /api/v1/feedback`

Ingest a batch of feedback as newline-delimited JSON (max 2 MB). Invalid lines are
quarantined, not dropped and not fatal. Re-posting identical bytes is a no-op.

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `authorization` | header | string | null | no |  |

Request body (`application/x-ndjson`): **string**

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | IngestOut |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |
| 413 | Request Entity Too Large | ErrorOut |

## `GET /api/v1/quarantine`

List Quarantine

| Parameter | In | Type | Required | Constraints / notes |
|---|---|---|---|---|
| `limit` | query | integer | no | minimum=1, maximum=100, default=20 |
| `cursor` | query | string | null | no |  |
| `reason` | query | string | null | no |  |
| `authorization` | header | string | null | no |  |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | QuarantinePage |
| 401 | Missing or invalid bearer token | ErrorOut |
| 403 | Token lacks the required role | ErrorOut |
| 404 | Not found | ErrorOut |
| 409 | Conflict | ErrorOut |
| 422 | Validation error | ErrorOut |
| 503 | A dependency is unavailable | ErrorOut |

## Schemas

### AddOp

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `op` | "add" | yes |  |
| `parent` | string | null | no | Path of the parent; null creates a new top-level domain |
| `name` | string | yes | pattern='^[a-z0-9][a-z0-9_]{0,62}$' |
| `title` | string | yes | minLength=1, maxLength=120 |

### AnnotationIn

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `node_ids` | integer[] | yes | maxItems=50; Most specific nodes; ancestors are added by the server. An empty list means 'no category applies'. |

### AnnotationOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `feedback_id` | integer | yes |  |
| `node_ids` | integer[] | yes | Stored label set, including implied ancestors |

### ChangeOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `version` | integer | yes |  |
| `op` | string | yes |  |
| `params` | object | yes |  |
| `labels_remapped` | integer | yes | Labels carried over automatically |
| `labels_flagged` | integer | yes | Labels queued for targeted human review |
| `replayed` | boolean | no | default=False; True if this Idempotency-Key had already been applied |
| `actor` | string | null | no |  |
| `created_at` | string | null | no |  |

### ChangesPage

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `items` | ChangeOut[] | yes |  |
| `next_cursor` | string | null | yes |  |

### ChildSpec

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `name` | string | yes | pattern='^[a-z0-9][a-z0-9_]{0,62}$' |
| `title` | string | yes | minLength=1, maxLength=120 |

### ClassifyIn

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `texts` | string[] | yes | minItems=1, maxItems=64 |
| `auto_threshold` | number | no | minimum=0.5, maximum=1.0, default=0.9; Confidence at or above which a result is routed automatically |

### ClassifyOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `model_version` | integer | yes |  |
| `results` | ClassifyResult[] | yes |  |

### ClassifyResult

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `labels` | LabelOut[] | yes | Predicted set; always closed under 'child implies parent' |
| `confidence` | number | yes |  |
| `route` | "auto" | "review" | yes |  |

### EfficiencyOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `live` | EfficiencyPoint[] | yes | One point per trained model version in this deployment |
| `simulation` | object | null | yes | Committed offline experiment (reports/label_efficiency.json) |

### EfficiencyPoint

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `model_version` | integer | yes |  |
| `n_labeled` | integer | yes |  |
| `hf1` | number | yes |  |
| `hf1_ci95` | any[] | null | yes |  |
| `status` | string | yes |  |
| `created_at` | string | yes |  |

### ErrorBody

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `code` | string | yes |  |
| `message` | string | yes |  |
| `details` | any | null | no |  |
| `request_id` | string | yes |  |

### ErrorOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `error` | ErrorBody | yes |  |

### HealthOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `status` | "ok" | "degraded" | "down" | yes |  |
| `checks` | object | yes |  |

### IngestOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `batch_id` | integer | yes |  |
| `n_records` | integer | yes |  |
| `n_accepted` | integer | yes |  |
| `n_quarantined` | integer | yes |  |
| `n_repaired` | integer | yes |  |
| `n_late` | integer | yes |  |
| `already_ingested` | boolean | yes |  |
| `quarantined_by_reason` | object | no | default={} |
| `job` | JobOut | null | yes | Embedding + scoring job for the new rows |

### JobIn

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `kind` | "retrain" | no | default='retrain' |

### JobOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `id` | integer | yes |  |
| `kind` | string | yes |  |
| `status` | "queued" | "running" | "succeeded" | "failed" | yes |  |
| `progress` | number | yes | 0..1, updated by the worker at each stage |
| `stage` | string | yes |  |
| `attempts` | integer | yes |  |
| `error` | string | null | yes |  |
| `result` | object | null | yes |  |
| `requested_by` | string | yes |  |
| `requested_at` | string | yes |  |
| `started_at` | string | null | yes |  |
| `finished_at` | string | null | yes |  |

### JobsPage

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `items` | JobOut[] | yes |  |
| `next_cursor` | string | null | yes |  |

### LabelOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `node_id` | integer | yes |  |
| `path` | string | yes |  |
| `prob` | number | yes |  |

### MergeOp

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `op` | "merge" | yes |  |
| `source` | string | yes | minLength=1, maxLength=400; Leaf node to retire |
| `target` | string | yes | minLength=1, maxLength=400; Node that receives its labels |

### ModelOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `id` | integer | yes |  |
| `status` | string | yes |  |
| `taxonomy_version` | integer | yes |  |
| `n_labeled` | integer | yes |  |
| `train_data_sha256` | string | yes |  |
| `code_version` | string | yes |  |
| `embed_model` | string | yes |  |
| `params` | object | yes |  |
| `metrics` | object | yes |  |
| `gate` | object | yes |  |
| `artifact_sha256` | string | yes |  |
| `created_at` | string | yes |  |

### ModelsPage

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `items` | ModelOut[] | yes |  |
| `next_cursor` | string | null | yes |  |

### MoveOp

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `op` | "move" | yes |  |
| `path` | string | yes | minLength=1, maxLength=400 |
| `new_parent` | string | null | yes |  |

### NodeMetric

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `node_id` | integer | yes |  |
| `path` | string | yes |  |
| `title` | string | yes |  |
| `depth` | integer | yes |  |
| `parent_id` | integer | null | yes |  |
| `support` | integer | yes | Held-out items that truly have this label |
| `predicted` | integer | yes |  |
| `precision` | number | null | yes | null when the node was never predicted |
| `recall` | number | null | yes | null when the node has no held-out examples |
| `f1` | number | yes |  |
| `precision_ci` | any[] | yes | minItems=2, maxItems=2 |
| `recall_ci` | any[] | yes | minItems=2, maxItems=2 |

### NodeMetricsOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `model_version` | integer | yes |  |
| `lang` | string | yes |  |
| `languages` | string[] | yes |  |
| `overall` | object | yes |  |
| `by_lang` | object | yes |  |
| `calibration` | object | yes |  |
| `nodes` | NodeMetric[] | yes |  |

### NodeOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `id` | integer | yes |  |
| `parent_id` | integer | null | yes |  |
| `name` | string | yes |  |
| `title` | string | yes |  |
| `path` | string | yes |  |
| `depth` | integer | yes |  |
| `n_labels` | integer | yes | Pool items labelled with this node |
| `n_review` | integer | yes | Items (pool + evaluation) whose label on this node is flagged for targeted relabelling |

### Principal

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `name` | string | yes |  |
| `role` | "viewer" | "annotator" | "admin" | yes |  |

### QuarantineItem

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `batch_id` | integer | yes |  |
| `line_no` | integer | yes |  |
| `reason` | string | yes |  |
| `detail` | string | yes |  |
| `payload_preview` | string | yes | First 300 bytes of the raw line, lossily decoded |
| `created_at` | string | yes |  |

### QuarantinePage

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `items` | QuarantineItem[] | yes |  |
| `next_cursor` | string | null | yes |  |

### QueueItem

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `id` | integer | yes |  |
| `text` | string | yes |  |
| `lang` | string | null | yes |  |
| `source` | string | yes |  |
| `created_at` | string | null | yes |  |
| `is_late` | boolean | yes |  |
| `split` | "pool" | "test" | yes | 'test' items are reference labels: admin-only, review mode only |
| `suggestions` | Suggestion[] | yes |  |
| `confidence` | number | null | yes | Calibrated P(the selected set is exactly right) |
| `uncertainty` | number | null | yes |  |
| `scored_by_model` | integer | null | yes | Model version that produced the suggestions |
| `current_node_ids` | integer[] | yes | Existing labels (review mode) |
| `review_reason` | string | null | yes |  |

### QueuePage

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `items` | QueueItem[] | yes |  |
| `next_cursor` | string | null | yes |  |
| `active_model` | integer | null | yes |  |
| `remaining` | integer | yes | Items left in this queue |

### RenameOp

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `op` | "rename" | yes |  |
| `path` | string | yes | minLength=1, maxLength=400 |
| `name` | string | yes | pattern='^[a-z0-9][a-z0-9_]{0,62}$' |
| `title` | string | null | no |  |

### RetireOp

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `op` | "retire" | yes |  |
| `path` | string | yes | minLength=1, maxLength=400 |

### SplitOp

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `op` | "split" | yes |  |
| `path` | string | yes | minLength=1, maxLength=400 |
| `children` | ChildSpec[] | yes | minItems=2, maxItems=20 |

### StatsOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `pool` | integer | yes |  |
| `test` | integer | yes |  |
| `labelled` | integer | yes |  |
| `needs_review` | integer | yes |  |
| `quarantined` | object | yes |  |
| `late` | integer | yes |  |
| `text_duplicates` | integer | yes |  |
| `by_lang` | object | yes |  |
| `taxonomy_version` | integer | null | yes |  |
| `active_model` | integer | null | yes |  |

### Suggestion

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `node_id` | integer | yes |  |
| `prob` | number | yes | Calibrated marginal probability |
| `selected` | boolean | yes | Part of the model's predicted label set (parent-consistent) |

### TaxonomyOut

| Field | Type | Required | Constraints / notes |
|---|---|---|---|
| `version` | integer | yes |  |
| `nodes` | NodeOut[] | yes |  |
