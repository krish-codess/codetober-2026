"""Post-training INT8 quantization with ONNX Runtime: calibration, per-layer sensitivity, and
mixed precision that leaves the most sensitive layers in float."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import onnx
import onnxruntime as ort
from onnxruntime.quantization import (
    CalibrationDataReader,
    CalibrationMethod,
    QuantFormat,
    QuantType,
    quantize_static,
)
from onnxruntime.quantization.shape_inference import quant_pre_process

logger = logging.getLogger("squeeze.quant")
METHODS = {"minmax": CalibrationMethod.MinMax, "entropy": CalibrationMethod.Entropy}
# Layers that carry weights. Sensitivity is measured, and precision chosen, per one of these.
LAYER_OPS = ("Conv", "Gemm", "MatMul")


def session(path: Path, threads: int = 0) -> ort.InferenceSession:
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.log_severity_level = 3
    return ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])


def to_input(x_u8: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(x_u8.transpose(0, 3, 1, 2), dtype=np.float32)


def logits(path: Path, x_u8: np.ndarray, idx: np.ndarray, batch: int = 50) -> np.ndarray:
    """Logits of the ONNX file itself (the artifact that ships) for x_u8[sorted(idx)]."""
    sess, idx = session(path), np.sort(idx)
    return np.concatenate(
        [sess.run(None, {"input": to_input(x_u8[idx[i : i + batch]])})[0] for i in range(0, len(idx), batch)]
    )


def prepare(src: Path, dst: Path) -> None:
    """Shape inference and graph clean-up. Node names in the result are the ones sensitivity and
    exclusion refer to."""
    quant_pre_process(str(src), str(dst), skip_symbolic_shape=True)


def layers(prepared: Path) -> list[tuple[str, str]]:
    return [(n.name, n.op_type) for n in onnx.load(str(prepared)).graph.node if n.op_type in LAYER_OPS]


class _Reader(CalibrationDataReader):  # type: ignore[misc]
    def __init__(self, x_u8: np.ndarray, batch: int = 16) -> None:
        self._batches = iter([{"input": to_input(x_u8[i : i + batch])} for i in range(0, len(x_u8), batch)])

    def get_next(self) -> dict[str, np.ndarray] | None:
        return next(self._batches, None)


def quantize(
    prepared: Path,
    dst: Path,
    calib_u8: np.ndarray,
    *,
    method: str = "minmax",
    only: list[str] | None = None,
    exclude: list[str] | None = None,
) -> None:
    """Static QDQ quantization: int8 weights per channel, uint8 activations, ranges from
    `calib_u8`. `only` quantizes just those nodes; `exclude` keeps those nodes in float32.

    uint8 activations are deliberate. With int8 activations ONNX Runtime 1.30 drops ReLUs it
    considers redundant after quantization, and on MobileNetV3 that alone takes dev top-1 from
    96% to 21% (measured; see docs/DECISIONS.md)."""
    quantize_static(
        str(prepared),
        str(dst),
        _Reader(calib_u8),
        quant_format=QuantFormat.QDQ,
        per_channel=True,
        activation_type=QuantType.QUInt8,
        weight_type=QuantType.QInt8,
        calibrate_method=METHODS[method],
        nodes_to_quantize=only or [],
        nodes_to_exclude=exclude or [],
    )


def kl(ref: np.ndarray, other: np.ndarray) -> float:
    """Mean KL(softmax(ref) || softmax(other)): how far the outputs moved, in nats. Far less
    noisy than a top-1 change on a few hundred images."""

    def log_softmax(z: np.ndarray) -> np.ndarray:
        z = z - z.max(1, keepdims=True)
        out: np.ndarray = z - np.log(np.exp(z).sum(1, keepdims=True))
        return out

    a, b = log_softmax(ref.astype(np.float64)), log_softmax(other.astype(np.float64))
    return float((np.exp(a) * (a - b)).sum(1).mean())


def sensitivity(
    prepared: Path, calib_u8: np.ndarray, dev_u8: np.ndarray, dev_labels: np.ndarray, limit: int | None = None
) -> list[dict[str, Any]]:
    """Quantize one layer at a time, leave everything else in float, and measure what that single
    layer costs on the dev split. Most sensitive first."""
    idx = np.arange(len(dev_u8))
    ref = logits(prepared, dev_u8, idx)
    ref_acc = float((ref.argmax(1) == dev_labels).mean())
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "one.onnx"
        for name, op in layers(prepared)[:limit]:
            quantize(prepared, out, calib_u8, only=[name])
            got = logits(out, dev_u8, idx)
            rows.append(
                {
                    "node": name,
                    "op_type": op,
                    "kl": kl(ref, got),
                    "top1_drop": ref_acc - float((got.argmax(1) == dev_labels).mean()),
                }
            )
            logger.info("sensitivity %s kl=%.5f", name, rows[-1]["kl"])
    rows.sort(key=lambda r: -r["kl"])
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows
