"""Uncertainty for accuracy numbers measured on a few hundred images."""

from __future__ import annotations

import math

import numpy as np


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion. Behaves at 0 and n, unlike the normal one."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - half) / (1 + z * z / n)), min(1.0, (centre + half) / (1 + z * z / n))


def paired_delta(
    correct_a: np.ndarray, correct_b: np.ndarray, seed: int = 0, rounds: int = 2000
) -> tuple[float, float, float]:
    """Accuracy of b minus accuracy of a on the same images, with a 95% paired bootstrap interval.
    Pairing matters: two models that disagree on 3 of 500 images differ by far less than two
    independent +-2 point intervals suggest."""
    d = correct_b.astype(np.float64) - correct_a.astype(np.float64)
    rng = np.random.default_rng(seed)
    means = d[rng.integers(0, len(d), (rounds, len(d)))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(d.mean()), float(lo), float(hi)


def accuracy(logits: np.ndarray, labels: np.ndarray) -> dict[str, float | int]:
    correct = int((logits.argmax(1) == labels).sum())
    lo, hi = wilson(correct, len(labels))
    return {"n": len(labels), "top1": correct / len(labels), "lo": lo, "hi": hi}
