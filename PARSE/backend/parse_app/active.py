"""Active-learning selection: which unlabelled items are worth a human's next minutes.

Strategies (all compared in reports/label_efficiency.json):

  random           the baseline every other strategy has to beat
  entropy_sum      total entropy of the tree-factorised joint. Kept as a documented NEGATIVE
                   result: the sum grows with the number of children, so it pulls ~70% of every
                   batch from the widest branch and ends up far below random.
  least_confident  the single least certain reachable node decision (max, not sum), so wide
                   branches earn no bonus
  mixed            half least_confident, spread over clusters so a batch is not near-duplicates,
                   half random so the labelled set stays representative. The service's default.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray
from sklearn.cluster import KMeans

from .hier import HierModel

Ints = NDArray[np.int64]
STRATEGIES = ("random", "entropy_sum", "least_confident", "mixed")
SHORTLIST_FACTOR = 6  # diversification clusters the k*6 most uncertain items into k groups


def diverse_top(x: NDArray[Any], scores: NDArray[Any], k: int, seed: int) -> Ints:
    """Positions (into x/scores) of k items: the most uncertain item of each of k clusters formed
    over the most uncertain k*SHORTLIST_FACTOR. Plain top-k uncertainty picks near-duplicates
    (the same complaint in five phrasings); one-per-cluster spends the batch on different things.
    Returned most uncertain first."""
    k = min(k, len(scores))
    if k == 0:
        return np.empty(0, dtype=np.int64)
    short = np.argsort(-scores, kind="stable")[: k * SHORTLIST_FACTOR]
    if len(short) <= k:
        return short
    clusters = KMeans(k, n_init=1, random_state=seed).fit_predict(x[short])
    picked: list[int] = []
    taken: set[int] = set()
    for pos, cl in zip(short, clusters, strict=True):  # short is sorted by uncertainty
        if cl not in taken:
            taken.add(int(cl))
            picked.append(int(pos))
    return np.array(picked, dtype=np.int64)


def mixed_order(x: NDArray[Any], scores: NDArray[Any], k: int, rng: np.random.Generator) -> Ints:
    """k positions: diversified-uncertain and uniformly random picks, interleaved (uncertain first)."""
    k = min(k, len(scores))
    uncertain = diverse_top(x, scores, (k + 1) // 2, seed=int(rng.integers(1 << 31)))
    rest = np.setdiff1d(np.arange(len(scores)), uncertain)
    random_part = rng.choice(rest, size=k - len(uncertain), replace=False)
    out = np.empty(k, dtype=np.int64)
    out[0::2] = uncertain
    out[1::2] = random_part
    return out


def select(
    strategy: str, model: HierModel | None, x: NDArray[Any], candidates: Ints, k: int, rng: np.random.Generator
) -> Ints:
    """Choose k of `candidates` (row indices into x). With no model yet, every strategy is random."""
    k = min(k, len(candidates))
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; expected one of {STRATEGIES}")
    if strategy == "random" or model is None:
        return rng.choice(candidates, size=k, replace=False)
    xc = x[candidates]
    if strategy == "entropy_sum":
        return candidates[np.argsort(-model.entropy(xc), kind="stable")[:k]]
    if strategy == "least_confident":
        return candidates[np.argsort(-model.uncertainty(xc), kind="stable")[:k]]
    return candidates[mixed_order(xc, model.uncertainty(xc), k, rng)]
