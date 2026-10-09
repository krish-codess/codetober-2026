# squeeze

Codetober: VISION.

Compress a vision model for edge boards and measure the real accuracy, latency and power
tradeoff, on the board, instead of estimating it.

A ResNet-50 teacher is pruned, a MobileNetV3-Small student is distilled from it, and both are
quantized to INT8. Every step is exported to ONNX and re-checked against an accuracy gate
before it counts. The surviving models are packed per device together with a single-file
benchmark harness; the harness uploads what it measured to an API, and a web explorer plots
accuracy against cost for each target.

## Status

Read this before running anything.

- **The code, tests, API reference and decisions log are here.** The measured results and the
  release packages are not: `results/raw/` and `release/` are empty in this repository.
- Because of that, **`docker compose up --build` fails from a clean clone** (the Dockerfile
  copies both directories), and the `bench` job in CI has no package to unpack.
- The numbers quoted in [docs/DECISIONS.md](docs/DECISIONS.md) come from runs on the author's
  laptop whose records were not committed. Treat them as the author's notes, not as evidence
  you can check here.
- No Raspberry Pi or Jetson was available. Those targets have packages, sensors and a deploy
  script implemented, and no measurements (decision B2).

The API and explorer do run without any of that, on an empty database; see below.

## What it does

| Stage | |
|---|---|
| Data | Imagenette (160 px), pinned by sha256. Every file is kept or listed in `quarantine.jsonl` with a reason. The evaluation set is the 500 images that are ImageNet *validation* images, because the dataset's own `val/` folder is mostly ImageNet training data that the pretrained models have already seen |
| Teacher | torchvision ResNet-50, classifier sliced to the ten classes, not trained |
| Pruning | Structured: inner channels of each bottleneck removed and the layers rebuilt smaller, then fine-tuned |
| Distillation | MobileNetV3-Small, reported three ways: pretrained, fine-tuned on hard labels, distilled from the teacher |
| Quantization | Static INT8. Every weight layer is quantized alone and ranked by how much it moves the outputs; the most sensitive stay in float32. The naive all-INT8 model is kept as a variant so the failure is on the chart |
| Gate | Teacher top-1 minus 2.0 points, checked on the exported ONNX file after every step. A step that fails is recorded as failed and not packaged |
| Package | One reproducible `tar.gz` per target: models, manifest with sha256s, 100 evaluation images, the harness |
| On the device | The harness refuses a model whose hash differs, re-runs the evaluation images and compares each prediction with the build host's, then times inference (p50/p95/p99) and reads watts from a real sensor, or records `power: null` and why |
| API | SQLite, rebuilt from the raw result files. Idempotent ingest with quarantine, tradeoff, per-layer sensitivity, package download, device upload |
| Explorer | One chart, accuracy against one cost at a time, with every mark in a table underneath |

Why each of these was chosen and what was rejected is in [docs/DECISIONS.md](docs/DECISIONS.md).
Every stored field is in [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md).

Targets are declared in [targets.json](targets.json): Raspberry Pi 5, Raspberry Pi 4,
Jetson Orin Nano, an x86 laptop (ONNX Runtime and OpenVINO), and CI runners.

## Run it

### The API and explorer

```bash
cd VISION
py -3.11 -m venv .venv && .venv/Scripts/activate     # or: python3 -m venv .venv && . .venv/bin/activate
pip install -e .
python -m squeeze serve                               # http://127.0.0.1:8000
```

With no results ingested the API answers and the explorer shows its empty state. The API alone
needs only FastAPI; to get the web UI, build it first (`cd web && npm ci && npm run build`);
it is served from `web/dist`.

### The pipeline

Needs the `ml` extra (PyTorch, ONNX Runtime, OpenVINO) and about 100 MB of dataset. On a
four-core laptop CPU the full run takes hours.

```bash
pip install -e ".[ml]" --extra-index-url https://download.pytorch.org/whl/cpu
curl -L -o imagenette.tgz https://s3.amazonaws.com/fast-ai-imageclas/imagenette2-160.tgz
tar -xzf imagenette.tgz
python -m squeeze data imagenette2-160     # validate, quarantine, split
python -m squeeze pipeline                 # add --smoke for a tiny run that checks the plumbing
python -m squeeze package                  # one package per target
python -m squeeze bench --target x86-laptop
python -m squeeze ingest                   # rebuild the database from the raw result files
```

Data, checkpoints and runs go to `SQUEEZE_DATA_DIR` (default `%LOCALAPPDATA%\vision-squeeze`
on Windows, `~/.cache/vision-squeeze` elsewhere). Keep it out of synced folders.

### On a board

```bash
deploy/deploy.sh squeeze-rpi5-<hash>.tar.gz pi@raspberrypi.local --power pmic
deploy/rollback.sh pi@raspberrypi.local
```

The board needs `python3` with `numpy` and `onnxruntime`. A package is unpacked into its own
directory and `current` is switched only after the checksum and the runtime have been verified,
so a failed deploy leaves the previous release in place. Untested on real boards.

## API

Generated from the code: [docs/openapi.json](docs/openapi.json); CI fails if the committed
file is stale. Reads are public. Uploading results needs the device token, which can only
append; the quarantine needs the admin token. Settings are in [.env.example](.env.example).

| | |
|---|---|
| `GET /v1/targets` | Targets with result and package counts |
| `GET /v1/tradeoff` | Accuracy against latency or energy for one target |
| `GET /v1/results` | Result rows, paged |
| `GET /v1/sensitivity` | Per-layer quantization sensitivity |
| `GET /v1/packages`, `/v1/packages/{id}/download` | Deployment packages |
| `POST /v1/results` | Device upload. A retry returns 200; the same id with different content returns 409 |
| `GET /healthz`, `/metrics` | Health with per-dependency checks, metrics |

## Tests

```bash
pip install -e ".[ml,dev]" --extra-index-url https://download.pytorch.org/whl/cpu
ruff check . && mypy && pytest -ra
cd web && npm ci && npm run typecheck && npm test
```

The Python suite includes a training smoke test, reproducible packaging, and the device
harness uploading to a real API process. The web suite covers the loading, empty, error,
stale and partial states and keyboard use. `npm run e2e` drives a running stack in a browser.

The workflow is [.github/workflows/vision.yml](../.github/workflows/vision.yml) at the
repository root. Its benchmark gate runs on GitHub's x86-64 and arm64 runners and compares
each model's latency *relative to the float student in the same job* with a committed
baseline, since absolute milliseconds on shared runners mean little.

## Layout

| Path | |
|---|---|
| `squeeze/data.py`, `synth.py` | Validation, quarantine, splits; a generator that plants every defect |
| `squeeze/pipeline.py`, `models.py`, `quant.py` | Pruning, distillation, quantization, the gate |
| `squeeze/package.py` | Reproducible per-target packages |
| `device/edge_bench.py` | The on-device harness; ships inside every package |
| `squeeze/db.py`, `api.py`, `schemas.py` | Storage and HTTP contract |
| `squeeze/regress.py`, `loadtest.py` | CI regression gate, API load test |
| `web/` | Explorer (React, TypeScript) |
| `deploy/` | Deploy to a board, roll back, verify a deployment from outside |
