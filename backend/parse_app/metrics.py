"""Evaluation: hierarchical P/R/F1, per-node counts, calibration error, grouped bootstrap."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .taxonomy import Bools, Tree

Floats = NDArray[np.float64]


def prf(tp: float, fp: float, fn: float) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 0.0
    return precision, recall, f1


def node_counts(y_true: Bools, y_pred: Bools) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.int64]]:
    return (y_true & y_pred).sum(0), (~y_true & y_pred).sum(0), (y_true & ~y_pred).sum(0)


def summary(y_true: Bools, y_pred: Bools, tree: Tree, macro_min_support: int = 10) -> dict[str, Any]:
    """Headline metric is hierarchical micro-F1 (hF1): micro-F1 over every (item, node) pair with
    ancestors included, so predicting the right parent of a wrong leaf earns partial credit."""
    tp, fp, fn = node_counts(y_true, y_pred)
    p, r, f1 = prf(tp.sum(), fp.sum(), fn.sum())
    out: dict[str, Any] = {
        "n": int(len(y_true)), "hf1": f1, "precision": p, "recall": r,
        "exact_match": float((y_true == y_pred).all(axis=1).mean()) if len(y_true) else 0.0,
        "consistent": float(tree.is_consistent(y_pred).mean()) if len(y_true) else 1.0,
    }  # fmt: skip
    for d in np.unique(tree.depth):
        cols = tree.depth == d
        out[f"f1_depth{d}"] = prf(tp[cols].sum(), fp[cols].sum(), fn[cols].sum())[2]
    leaf = tree.is_leaf
    out["f1_leaf"] = prf(tp[leaf].sum(), fp[leaf].sum(), fn[leaf].sum())[2]
    supported = (tp + fn) >= macro_min_support
    out["macro_f1"] = (
        float(np.mean([prf(tp[j], fp[j], fn[j])[2] for j in np.flatnonzero(supported)])) if supported.any() else 0.0
    )
    out["macro_nodes"] = int(supported.sum())
    return out


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval for a proportion; (0, 1) when there is no evidence at all."""
    if n == 0:
        return 0.0, 1.0
    phat = successes / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def ece(confidence: Floats, correct: NDArray[Any], bins: int = 10) -> float:
    """Expected calibration error: bin-weighted |mean confidence - observed accuracy|."""
    if len(confidence) == 0:
        return 0.0
    edges = np.linspace(0, 1, bins + 1)
    which = np.clip(np.digitize(confidence, edges[1:-1]), 0, bins - 1)
    total = 0.0
    for b in range(bins):
        m = which == b
        if m.any():
            total += m.mean() * abs(confidence[m].mean() - correct[m].mean())
    return float(total)


def reliability(confidence: Floats, correct: NDArray[Any], bins: int = 10) -> list[dict[str, float]]:
    edges = np.linspace(0, 1, bins + 1)
    which = np.clip(np.digitize(confidence, edges[1:-1]), 0, bins - 1)
    return [
        {"lo": float(edges[b]), "hi": float(edges[b + 1]), "n": int((which == b).sum()),
         "confidence": float(confidence[which == b].mean()), "accuracy": float(correct[which == b].mean())}
        for b in range(bins) if (which == b).any()
    ]  # fmt: skip


def bootstrap_hf1(
    y_true: Bools, y_pred: Bools, groups: NDArray[Any], n_boot: int = 300, seed: int = 0
) -> tuple[float, float]:
    """95% CI for hF1, resampling whole groups: translations of one sentence are not independent."""
    rng = np.random.default_rng(seed)
    _, inv = np.unique(groups, return_inverse=True)
    n_groups = inv.max() + 1
    tp = np.bincount(inv, (y_true & y_pred).sum(1), n_groups)
    fp = np.bincount(inv, (~y_true & y_pred).sum(1), n_groups)
    fn = np.bincount(inv, (y_true & ~y_pred).sum(1), n_groups)
    stats = []
    for _ in range(n_boot):
        s = rng.integers(0, n_groups, n_groups)
        stats.append(prf(tp[s].sum(), fp[s].sum(), fn[s].sum())[2])
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return float(lo), float(hi)
