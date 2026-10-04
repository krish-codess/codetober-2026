"""Active-learning selection: which unlabelled items are worth a human's next minutes."""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray
from sklearn.cluster import KMeans

from .hier import HierModel

Ints = NDArray[np.int64]
STRATEGIES = ("random", "entropy", "entropy_diverse")
SHORTLIST_FACTOR = 6  # entropy_diverse clusters the k*6 most uncertain items into k groups


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


def select(
    strategy: str, model: HierModel | None, x: NDArray[Any], candidates: Ints, k: int, rng: np.random.Generator
) -> Ints:
    """Choose k of `candidates` (row indices into x). With no model yet, every strategy is random."""
    k = min(k, len(candidates))
    if strategy == "random" or model is None:
        return rng.choice(candidates, size=k, replace=False)
    scores = model.entropy(x[candidates])
    if strategy == "entropy":
        return candidates[np.argsort(-scores, kind="stable")[:k]]
    if strategy == "entropy_diverse":
        return candidates[diverse_top(x[candidates], scores, k, seed=int(rng.integers(1 << 31)))]
    raise ValueError(f"unknown strategy {strategy!r}; expected one of {STRATEGIES}")
