# Decisions

What was chosen, what was rejected, and why. Numbers quoted here were measured in this repository;
the run records are in `results/raw/`.

## Data

**D1. Evaluate only on images no model has seen (provenance split).**
Imagenette's own `val/` folder is not a held-out set for an ImageNet-pretrained model: it was
re-split 70/30 from ImageNet, so 3,791 of its 3,925 images are ImageNet *training* images, and
366 ImageNet *validation* images sit in `train/`. The teacher and the student both start from
ImageNet weights. The evaluation set is therefore the 500 images whose filename carries
`ILSVRC2012_val_` (exactly 50 per class), wherever they sit; everything else is the training pool.
*Rejected:* the dataset's folders (leaks in both directions); a random split (same leak).
*Cost:* 500 images gives a 95% interval of about ±1.5 points, so every accuracy is reported with a
Wilson interval, and step-to-step changes use a paired bootstrap on the same images.

**D2. Three more splits, by filename hash.** From the training pool: `dev` 8% (every choice made
during quantization: sensitivity ranking, how many layers stay float), `calib` 4% (calibration
only), `train` the rest. A hash of the filename decides, so a file added later never moves an
existing file. Choices are never made on `eval`. Subsets ("first N") are taken in hash order
because the files are stored class by class: an early run took the first 4,000 training images,
got four of the ten classes, and fine-tuning dropped the student from 95.8% to 82.4%. There is a
test for it now.

**D3. Validate at the boundary, quarantine, never drop.** Every file is either kept or listed in
`quarantine.jsonl` with a reason (`not_an_image`, `unknown_class`, `label_conflict`, `empty`,
`undecodable`, `too_small`, `duplicate_of`). Repairable defects are repaired and flagged
(242 greyscale JPEGs converted; 32 images with aspect ratio above 3 flagged). The real archive
quarantines 3 files (two `.DS_Store`, one CSV). The generator plants every defect and a test checks
each lands where it should and that kept + quarantined = files on disk.

**D4. "Deployment conditions" are simulated, and labelled as such.** There is no camera in this
project. `data.deploy_conditions` darkens, blurs, adds sensor noise and re-encodes as a hard JPEG,
seeded per image. It is used for half of the calibration set and for a second accuracy column.
It is a stand-in for a Pi camera, not a measurement of one.

**D5. The fast.ai bucket moved.** The URL torchvision and most tutorials use returns
`NoSuchBucket`. The archive is fetched from the current bucket and pinned by sha256.

## Modelling

**M1. Teacher: torchvision ResNet-50 (IMAGENET1K_V2), restricted to the ten classes** by slicing
the classifier rows. No training, so the teacher's eval images are provably unseen.
98.2% (96.6 to 99.1) on eval, about 2.1 GFLOPs at 160 px.

**M2. Student: MobileNetV3-Small, same slicing, then fine-tuned.** Three students are reported so
distillation is compared with honest baselines: the pretrained network untouched, fine-tuned on
hard labels, and distilled from the teacher (T=4, alpha=0.7). Baselines below all of them: majority
class and logistic regression on 8x8 pixels.

**M3. Structured pruning, not unstructured.** Inner channels of every ResNet bottleneck are removed
by L1 norm x BatchNorm scale, and the layers are rebuilt smaller. Unstructured (magnitude) pruning
was not built: ONNX Runtime and OpenVINO run dense kernels, so zeros buy no latency on any target
here. Only the two inner widths of a block are pruned, so no residual connection changes shape.
*Not built:* global ranking across blocks, pruning the residual stream.

**M4. Accuracy gate = teacher top-1 minus 2.0 points on eval, checked after every step on the
exported ONNX file** (not on the PyTorch module that produced it). Export is itself checked:
max |logit| difference between PyTorch and ONNX Runtime on 16 images must be below 1e-3.
A step that fails is recorded as failed and is not packaged. The first full run's distilled
student scored 96.0% against a 96.2% floor. The gate was left alone and the student was trained
longer on more data instead (second run); both runs are in `results/raw/`.

**M5. Quantization: static QDQ, uint8 activations, int8 per-channel weights.**
Found by bisection on MobileNetV3, all on the dev split:
- int8 activations lose the model (dev 96% to 21%) with every op type quantized, and are fine when
  ReLU is excluded or with `QDQKeepRemovableActivations`: ONNX Runtime 1.30 removes ReLUs it
  considers redundant under int8 activations, and here they are not. uint8 activations avoid it.
- The first convolution and the first depthwise convolution are each catastrophic alone
  (KL 3.7 and 3.9 nats; every other layer below 0.05): after BatchNorm folding their output
  channels differ in range by three orders of magnitude and one per-tensor scale cannot serve them.
So the pipeline measures every weight layer alone (`quant.sensitivity`), ranks by KL divergence of
the outputs (far less noisy than top-1 on 400 images), and keeps the top k in float32, with k the
smallest value that keeps dev top-1 within 1 point of float. Both the naive and the mixed model are
kept as variants, so the failure is on the curve, not hidden.
*Rejected:* quantization-aware training (needs GPU-days this project does not have),
cross-layer equalization (would fix the root cause; not built).

**M6. Calibration data is an experiment, not an assumption.** The same model is calibrated on
uniform noise, 8/32/128 clean images, deploy-condition images, and a half/half mix, with min-max
and entropy; results are in the run record and the README.

## Measurement

**B1. Nothing is estimated.** The harness reads watts from a sensor (Pi 5 PMIC, hwmon/INA2xx, RAPL,
a laptop battery gauge, or any command that prints watts) or records `power: null` with the reason.
Energy per inference = (median load watts - median idle watts) x seconds per inference.
`--power-scale` / `--power-offset-w` exist because every cheap sensor needs calibrating against a
reference meter.

**B2. What "the actual target hardware" means here.** The author had no Raspberry Pi during the
build. Measured for real: the laptop (x86-64, and its battery gauge when unplugged) and, in CI,
GitHub's arm64 runners (real aarch64 silicon, the Pi's instruction set, not an emulator, but not a
Pi and with no power sensor). The Pi 4 / Pi 5 / Jetson targets have packages, a deploy script and
a harness with their sensors implemented, and **no measurements**; the explorer says "not measured"
for them. QEMU was rejected: emulated latency is not latency.

**B3. TensorRT and OpenVINO.** OpenVINO runs the same ONNX files (float and QDQ) and is benchmarked
on x86. TensorRT has no code path of its own: on a Jetson the harness uses ONNX Runtime's
TensorRT execution provider (`--providers TensorrtExecutionProvider,CUDAExecutionProvider`), which
builds the engine on the device, where engines must be built anyway. It is untested (no Jetson).
*Rejected:* hand-written `trtexec` scripts nobody here could run.

**B4. The harness verifies before it times.** It refuses a model whose sha256 differs from the
manifest and re-runs the bundled 100 evaluation images, comparing each prediction with the build
host's. INT8 kernels round differently across CPUs; this is where that would show.

**B5. CI gate on ratios, not milliseconds.** Shared runners vary run to run, so the gate is
(a) accuracy on the evaluation pack, which is deterministic, and (b) each model's p50 relative to
the float student in the same job, compared with a committed baseline for the same CPU model,
+30% allowed. A CPU without a baseline is accuracy-gated only and prints its ratios.
*Known weakness:* GitHub hands out several CPU models, so the latency half of the gate is only as
good as the baselines committed. A self-hosted Pi runner (job `bench-device`) is the real answer.

## Storage and API

**S1. SQLite.** One writer (ingest), read-mostly, thousands of rows, one node. The database is
derived: `results/raw/*.json[l]` is the source of truth and `squeeze ingest` rebuilds it, so backup
is "keep the raw files". PostgreSQL would add a service for nothing at this size.
*Ceiling:* see README, "Where it breaks at 10x".

**S2. Results join to models by content hash**, not by foreign key. A device result names the
sha256 of the file it ran. Results that arrive before their pipeline run are stored and join up
later (`known_model` in `/v1/results`).

**S3. Idempotency key = hash of the result.** A retry returns 200; the same id with different
content returns 409. Bodies are canonicalised before hashing so a file upload and an HTTP upload of
the same result agree.

**S4. Auth.** Reads are public (it is a results dashboard). Uploading needs the device token, which
can only append results; the quarantine needs the admin token. Unset tokens match nothing.
*Not built:* per-device tokens, users, tenants.

**S5. The pipeline is a batch job, not an endpoint.** Training does not belong behind an HTTP
request. The API serves what the pipeline and the devices produced.

**S6. One container.** The API process serves the built UI. No nginx, no second image.

## Interface

**U1. One chart, one axis pair.** Accuracy against one cost at a time (p50, p95 or energy), log
x-axis, never two y-scales. Colour is the network family (three hues, validated for colour-vision
deficiency in light and dark as an all-pairs set); shape is the precision; failed variants are
hollow and say so in text; every mark is in the table underneath.

**U2. Cache with ETags, fall back to the last good answer.** Each URL is fetched once per view and
revalidated with `If-None-Match` when the window regains focus. If the server is unreachable the
page keeps the cached data under a banner that says how old it is.

## Deliberately not built

Quantization-aware training; unstructured pruning; a Postgres service; QEMU "benchmarks";
per-device credentials; a job queue for the pipeline; object storage for packages (large ones are
rebuilt, byte-identical, from the run); synthetic numbers for boards that were never plugged in.
