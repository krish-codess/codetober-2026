# Data dictionary

SQLite, schema in `squeeze/db.py` (`MIGRATIONS`, applied in order, version in `PRAGMA user_version`).
The database is derived from `results/raw/`; `python -m squeeze ingest` rebuilds it.
All timestamps are UTC ISO 8601 text. Accuracies are fractions in [0, 1].

## run: one execution of the optimisation pipeline

| Column | Type | Null | Meaning |
|---|---|---|---|
| run_id | text PK | no | first 12 hex of sha256(configuration + dataset version) |
| created_at | text | no | when the run's first step finished |
| git_commit | text | no | commit of the code that ran |
| dataset_version | text | no | hash over (file sha1, label, split) of every kept image |
| teacher_top1 | real | no | teacher accuracy on eval; the gate is relative to it |
| budget_pt | real | no | accuracy budget in percentage points |
| config | json | no | every pipeline setting |
| baselines | json | no | majority class and logistic regression results |
| calibration_study | json | no | accuracy per calibration source / size / method |

## variant: one model produced by one step of a run

Primary key (run_id, name). `run_id` references `run` with cascade delete.

| Column | Type | Null | Meaning |
|---|---|---|---|
| name | text | no | e.g. `student-kd-int8-mixed` |
| parent | text | yes | variant this step started from; null for a starting point |
| technique | text | no | none, architecture, finetune, distillation, pruning, pruning+finetune, quantization, quantization-mixed |
| arch | text | no | network architecture |
| precision | text | no | `fp32`, `int8`, or `int8+fp32` (mixed) |
| params | integer | no | parameter count |
| size_bytes | integer | no | size of the ONNX file |
| model_sha256 | text | no | sha256 of the ONNX file; device results join on it |
| top1, top1_lo, top1_hi | real | no | eval accuracy and its 95% Wilson interval |
| n_eval | integer | no | number of eval images (500) |
| top1_deploy | real | no | accuracy on the same images under simulated camera conditions |
| parent_agree | real | yes | share of eval predictions identical to the parent's |
| parent_delta, _lo, _hi | real | yes | accuracy minus parent's, with a 95% paired bootstrap interval |
| gate | text | no | `pass` or `fail` against teacher_top1 - budget_pt |
| gate_reason | text | no | the numbers behind the gate decision |
| detail | json | no | step specifics: loss per epoch, float layers, export parity, calibration |
| position | integer | no | order within the run |

## sensitivity: one layer quantized alone

Primary key (run_id, variant, node).

| Column | Type | Null | Meaning |
|---|---|---|---|
| variant | text | no | the float model that was scanned |
| node | text | no | ONNX node name |
| op_type | text | no | Conv, Gemm or MatMul |
| rank | integer | no | 1 = most sensitive |
| kl | real | no | mean KL(float outputs ‖ outputs with this layer INT8), nats, dev split |
| top1_drop | real | no | dev top-1 lost by quantizing this layer alone (fraction) |
| kept_float | 0/1 | no | 1 if the mixed-precision model leaves it in float32 |

## bench: one model benchmarked on one device

Written by `device/edge_bench.py`. Primary key `result_id`.
Indexes: `bench_by_target (target, runtime, synthetic, variant)` for the explorer;
`bench_by_arrival (received_at DESC, result_id DESC)` for keyset pagination.

| Column | Type | Null | Unit / meaning |
|---|---|---|---|
| result_id | text PK | no | hash of the result; the idempotency key |
| body_sha256 | text | no | hash of the canonical body, to tell a retry from a conflict |
| received_at | text | no | when this server stored it |
| measured_at | text | no | when the device measured it (device clock) |
| source | text | no | `api` or the raw file it was read from |
| target | text | no | a name from `targets.json` |
| variant, model_sha256 | text | no | which model; joins to `variant` (no FK: results may arrive first) |
| runtime, runtime_version, provider | text | no | e.g. onnxruntime 1.30.0 CPUExecutionProvider |
| threads | integer | no | intra-op threads; 0 = runtime default |
| machine, cpu_model, cores, os | | no | device fingerprint |
| board | text | yes | `/proc/device-tree/model` where it exists |
| n | integer | no | timed single-image inferences |
| p50_ms, p95_ms, p99_ms, mean_ms | real | no | milliseconds per inference, batch 1, inference only |
| n_acc | integer | no | evaluation images re-run on the device |
| top1_device | real | no | accuracy on those images |
| agree_host | real | no | share of predictions equal to the build host's |
| idle_w, load_w | real | yes | watts, median over the window; null without a sensor |
| energy_mj | real | yes | millijoules per inference, net of idle |
| power_source | text | yes | sensor used |
| power_unavailable | text | yes | why there is no power figure |
| synthetic | 0/1 | no | 1 for generated load-test rows; never shown unless asked for |

## package: one deployment archive

| Column | Type | Null | Meaning |
|---|---|---|---|
| package_id | text PK | no | sha256 of the archive |
| name, filename | text | no | `squeeze-<target>-<run_id>[.tar.gz]` |
| target, run_id | text | no | what it was built for and from |
| size_bytes | integer | no | archive size |
| git_commit, dataset_version | text | no | provenance |
| variants | json | no | models inside: name, precision, size, host accuracy |

## quarantine: uploads that failed validation

| Column | Type | Null | Meaning |
|---|---|---|---|
| id | integer PK | no | arrival order |
| body_sha256 | text unique | no | the same bad upload is stored once |
| received_at, source | text | no | when and from where |
| errors | json | no | list of {loc, msg} |
| body | text | no | the upload as received (first 20,000 characters) |

## Files outside the database

| Path | Meaning |
|---|---|
| `results/raw/pipeline-<run>.json` | run record; immutable |
| `results/raw/bench-*.jsonl` | device results, one per line; append-only |
| `results/raw/uploaded.jsonl` | results accepted over HTTP; append-only |
| `results/raw/*.package.json` | package records |
| `<data dir>/dataset/images.u8` | uint8 N x 160 x 160 x 3 cache of every kept image |
| `<data dir>/dataset/index.json` | per image: path, label, split, sha1, flags |
| `<data dir>/dataset/quarantine.jsonl` | per rejected file: path, reason, bytes |
| `<data dir>/runs/<run>/` | step records, ONNX files, logits |
