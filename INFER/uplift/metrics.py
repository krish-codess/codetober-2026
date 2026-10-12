"""Uplift evaluation. Nothing in here is a classification metric: a unit's own effect is never observed, so every
number is a contrast between treated and control units that a model ranked alike.

Conventions: `y` outcome, `t` treatment in {0, 1}, `score` higher = treat first. Curves are indexed by the fraction
of the population targeted, start at (0, 0) and END AT THE AVERAGE TREATMENT EFFECT, so curves from different
models on the same data share both endpoints and differ only in how early they collect the effect.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

F = NDArray[np.float64]
Z95 = 1.959963984540054


def order(score: F, seed: int = 0) -> NDArray[np.intp]:
    """Indices by descending score. Ties are broken at random (seeded), never by input order: a model that scores
    everyone the same must look like random targeting, not like whatever order the rows arrived in."""
    perm = np.random.default_rng(seed).permutation(len(score))
    return perm[np.argsort(-score[perm], kind="stable")]


def _cumulative(y: F, t: F, score: F) -> tuple[F, F, F, F]:
    o = order(score)
    y, t = y[o].astype(float), t[o].astype(float)
    return np.cumsum(t), np.cumsum(1 - t), np.cumsum(y * t), np.cumsum(y * (1 - t))


def qini_curve(y: F, t: F, score: F) -> tuple[F, F]:
    """Radcliffe's Qini curve: Q(k) = Y_T(k) - Y_C(k) * N_T(k) / N_C(k) over the top k, per treated unit."""
    nt, nc, yt, yc = _cumulative(y, t, score)
    q = yt - yc * np.divide(nt, nc, out=np.zeros_like(nt), where=nc > 0)
    n = len(y)
    return np.arange(n + 1) / n, np.concatenate([[0.0], q / max(nt[-1], 1.0)])


def uplift_curve(y: F, t: F, score: F) -> tuple[F, F]:
    """Cumulative uplift: (mean treated - mean control within the top k) * k / n, the per-capita gain of treating
    exactly the top k."""
    nt, nc, yt, yc = _cumulative(y, t, score)
    diff = np.divide(yt, nt, out=np.zeros_like(nt), where=nt > 0) - np.divide(
        yc, nc, out=np.zeros_like(nc), where=nc > 0
    )
    n = len(y)
    frac = np.arange(n + 1) / n
    return frac, np.concatenate([[0.0], diff * frac[1:]])


def area_over_random(frac: F, curve: F) -> float:
    """Area between a curve and the straight line to its endpoint (what random targeting earns)."""
    return float(np.trapezoid(curve, frac) - curve[-1] / 2)


def qini(y: F, t: F, score: F) -> float:
    return area_over_random(*qini_curve(y, t, score))


def auuc(y: F, t: F, score: F) -> float:
    return area_over_random(*uplift_curve(y, t, score))


def diff_in_means(y: F, t: F) -> dict[str, float]:
    """Observed uplift in a group of a randomized experiment, with a 95% normal interval and two-sided p-value."""
    yt, yc = y[t == 1], y[t == 0]
    if len(yt) < 2 or len(yc) < 2:
        return {
            "uplift": 0.0,
            "ci_low": -math.inf,
            "ci_high": math.inf,
            "p": 1.0,
            "n_treated": len(yt),
            "n_control": len(yc),
        }
    d = float(yt.mean() - yc.mean())
    se = math.sqrt(float(yt.var(ddof=1)) / len(yt) + float(yc.var(ddof=1)) / len(yc))
    p = math.erfc(abs(d) / se / math.sqrt(2)) if se > 0 else (0.0 if d else 1.0)
    return {
        "uplift": d,
        "ci_low": d - Z95 * se,
        "ci_high": d + Z95 * se,
        "p": p,
        "n_treated": len(yt),
        "n_control": len(yc),
    }


def uplift_at(y: F, t: F, score: F, fraction: float) -> float:
    """Observed uplift among the top `fraction` by score."""
    top = order(score)[: max(1, round(fraction * len(y)))]
    return diff_in_means(y[top], t[top])["uplift"]


def deciles(y: F, t: F, score: F, true_uplift: F | None = None) -> list[dict[str, float | int | None]]:
    """Predicted vs observed uplift by score decile (1 = highest predicted). This is the calibration check:
    a policy that thresholds on predicted uplift is only as good as this table is monotone and on the diagonal."""
    out: list[dict[str, float | int | None]] = []
    for i, idx in enumerate(np.array_split(order(score), 10), start=1):
        d = diff_in_means(y[idx], t[idx])
        out.append(
            {
                "decile": i,
                "n_treated": int(d["n_treated"]),
                "n_control": int(d["n_control"]),
                "mean_predicted": float(score[idx].mean()),
                "observed_uplift": d["uplift"],
                "ci_low": d["ci_low"],
                "ci_high": d["ci_high"],
                "true_uplift": None if true_uplift is None else float(true_uplift[idx].mean()),
            }
        )
    return out


def bootstrap_qini(y: F, t: F, scores: dict[str, F], rounds: int, seed: int) -> dict[str, F]:
    """Qini of every model on the SAME resamples, so differences between models are paired."""
    rng = np.random.default_rng(seed)
    out = {name: np.empty(rounds) for name in scores}
    for b in range(rounds):
        idx = rng.integers(0, len(y), len(y))
        for name, s in scores.items():
            out[name][b] = qini(y[idx], t[idx], s[idx])
    return out


def thin(frac: F, curve: F, points: int = 101) -> list[list[float]]:
    """A curve at `points` evenly spaced fractions, for storage and plotting."""
    idx = np.round(np.linspace(0, len(frac) - 1, points)).astype(int)
    return [[round(float(frac[i]), 4), round(float(curve[i]), 6)] for i in idx]


def benjamini_hochberg(p: F) -> F:
    """Adjusted p-values controlling the false discovery rate across a family of segment tests."""
    m = len(p)
    o = np.argsort(p)
    adj = np.minimum.accumulate((p[o] * m / np.arange(1, m + 1))[::-1])[::-1]
    out = np.empty(m)
    out[o] = np.minimum(adj, 1.0)
    return out
