"""Hierarchical multi-label classifier over fixed embeddings.

One logistic regression per taxonomy node, trained only on the examples where the node's parent
is positive, so it models P(node | parent). The marginal is the product down the path:

    P(node) = P(node | parent) * P(parent)

which is <= P(parent) by construction. After per-depth calibration the inequality is re-imposed
top-down, and `decode` only emits a node if its parent was emitted. A prediction that violates
the taxonomy therefore cannot be produced, whatever the weights are.

`hierarchical=False` gives the flat baseline: every node trained on all examples, no product, no
enforcement.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import KFold

from .taxonomy import Bools, Tree

Floats = NDArray[np.float64]
EPS = 1e-6


def _sigmoid(z: Floats) -> Floats:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _logit(p: Floats) -> Floats:
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


@dataclass(frozen=True)
class HierModel:
    tree: Tree
    mu: Floats  # feature mean subtracted before the linear layer
    w: Floats  # (n_nodes, dim)
    b: Floats  # (n_nodes,)
    trained: Bools  # False = node had no usable examples; it predicts its prior
    hierarchical: bool = True
    cal_a: Floats = field(default_factory=lambda: np.ones(8))  # per-depth Platt slope
    cal_b: Floats = field(default_factory=lambda: np.zeros(8))  # per-depth Platt intercept
    conf_a: float = 1.0  # Platt for item-level confidence (P(predicted set exactly right))
    conf_b: float = 0.0
    threshold: float = 0.5

    def conditional(self, x: NDArray[Any]) -> Floats:
        """P(node | parent) for every node; shape (n, n_nodes)."""
        return _sigmoid((x.astype(np.float64) - self.mu) @ self.w.T + self.b)

    def marginals(self, x: NDArray[Any], calibrated: bool = True) -> Floats:
        p = self.conditional(x)
        parent = self.tree.parent
        if self.hierarchical:
            for j in range(len(self.tree)):  # topological order: parent is already a marginal
                if parent[j] >= 0:
                    p[:, j] *= p[:, parent[j]]
        if calibrated:
            d = self.tree.depth
            p = _sigmoid(_logit(p) * self.cal_a[d] + self.cal_b[d])
        if self.hierarchical:
            for j in range(len(self.tree)):
                if parent[j] >= 0:
                    np.minimum(p[:, j], p[:, parent[j]], out=p[:, j])
        return p

    def decode(self, p: Floats) -> Bools:
        y = p >= self.threshold
        if self.hierarchical:
            parent = self.tree.parent
            for j in range(len(self.tree)):
                if parent[j] >= 0:
                    y[:, j] &= y[:, parent[j]]
        return y

    def raw_confidence(self, p: Floats) -> Floats:
        """Per item: how far the least certain node decision is from the threshold, in [0.5, 1]."""
        least_certain: Floats = np.maximum(p, 1 - p).min(axis=1)
        return least_certain

    def confidence(self, p: Floats) -> Floats:
        """Calibrated probability that the decoded label set is exactly right."""
        return _sigmoid(_logit(self.raw_confidence(p)) * self.conf_a + self.conf_b)

    def entropy(self, x: NDArray[Any]) -> Floats:
        """Entropy (nats) of the tree-factorised joint: sum_j P(parent_j) * H(P(j | parent_j)).
        A node only contributes uncertainty to the extent its parent is believed to apply."""
        c = np.clip(self.conditional(x), EPS, 1 - EPS)
        h = -(c * np.log(c) + (1 - c) * np.log(1 - c))
        if not self.hierarchical:
            flat: Floats = h.sum(axis=1)
            return flat
        reach = np.ones_like(c)
        parent = self.tree.parent
        marg = c.copy()
        for j in range(len(self.tree)):
            if parent[j] >= 0:
                reach[:, j] = marg[:, parent[j]]
                marg[:, j] *= marg[:, parent[j]]
        total: Floats = (reach * h).sum(axis=1)
        return total

    # --- artifact: plain arrays in an .npz, never pickle -----------------------------------
    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        np.savez_compressed(
            buf, node_ids=self.tree.node_ids, parent=self.tree.parent, mu=self.mu.astype(np.float32),
            w=self.w.astype(np.float32), b=self.b.astype(np.float32), trained=self.trained,
            cal_a=self.cal_a, cal_b=self.cal_b,
            scalars=np.array([self.conf_a, self.conf_b, self.threshold, float(self.hierarchical)]),
        )  # fmt: skip
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes) -> HierModel:
        z = np.load(io.BytesIO(data), allow_pickle=False)
        conf_a, conf_b, threshold, hierarchical = (float(v) for v in z["scalars"])
        return cls(
            tree=Tree(z["node_ids"], z["parent"]), mu=z["mu"].astype(np.float64), w=z["w"].astype(np.float64),
            b=z["b"].astype(np.float64), trained=z["trained"], hierarchical=bool(hierarchical),
            cal_a=z["cal_a"], cal_b=z["cal_b"], conf_a=conf_a, conf_b=conf_b, threshold=threshold,
        )  # fmt: skip


def fit(
    x: NDArray[Any], y: Bools, tree: Tree, *, c: float = 30.0, hierarchical: bool = True, mu: Floats | None = None
) -> HierModel:
    """`y` must be ancestor-closed (Tree.close). `mu` defaults to the mean of `x`."""
    x = x.astype(np.float64)
    mu = x.mean(axis=0) if mu is None else mu
    xc = x - mu
    n_nodes, dim = len(tree), x.shape[1]
    w, b, trained = np.zeros((n_nodes, dim)), np.zeros(n_nodes), np.zeros(n_nodes, dtype=bool)
    for j in range(n_nodes):
        p = tree.parent[j]
        rows = y[:, p] if (hierarchical and p >= 0) else np.ones(len(y), dtype=bool)
        target = y[rows, j]
        n, pos = int(rows.sum()), int(target.sum())
        if pos == 0 or pos == n:
            # No contrast to learn from: predict the smoothed base rate. An unseen node keeps a
            # small non-zero probability, which is what lets uncertainty sampling discover it.
            b[j] = _logit(np.array((pos + 0.5) / (n + 1.0)))
            continue
        lr = LogisticRegression(C=c, max_iter=300)
        lr.fit(xc[rows], target)
        w[j], b[j], trained[j] = lr.coef_[0], lr.intercept_[0], True
    return HierModel(tree=tree, mu=mu, w=w, b=b, trained=trained, hierarchical=hierarchical)


def _platt(score: Floats, target: Bools) -> tuple[float, float]:
    if target.all() or not target.any() or len(target) < 10:
        return 1.0, 0.0
    lr = LogisticRegression(C=1e4, max_iter=200)
    lr.fit(score.reshape(-1, 1), target)
    return float(lr.coef_[0, 0]), float(lr.intercept_[0])


def fit_calibrated(
    x: NDArray[Any], y: Bools, tree: Tree, *, c: float = 30.0, folds: int = 4, seed: int = 0,
    mu: Floats | None = None, hierarchical: bool = True,
) -> HierModel:  # fmt: skip
    """Fit on everything; calibrate on out-of-fold predictions so no example calibrates itself.

    Three things are learned from the out-of-fold marginals:
      * per-depth Platt scaling of node probabilities,
      * the single decision threshold that maximises micro-F1,
      * Platt scaling of item confidence against "was the whole label set right".
    """
    model = fit(x, y, tree, c=c, mu=mu, hierarchical=hierarchical)
    if len(x) < 5 * folds:
        return model
    oof = np.zeros(y.shape)
    for train, held in KFold(folds, shuffle=True, random_state=seed).split(x):
        oof[held] = fit(x[train], y[train], tree, c=c, mu=model.mu, hierarchical=hierarchical).marginals(
            x[held], calibrated=False
        )
    cal_a, cal_b = np.ones(8), np.zeros(8)
    for d in np.unique(tree.depth):
        cols = tree.depth == d
        cal_a[d], cal_b[d] = _platt(_logit(oof[:, cols]).ravel(), y[:, cols].ravel())
    model = replace(model, cal_a=cal_a, cal_b=cal_b)

    # Re-derive calibrated out-of-fold marginals exactly as `marginals` would produce them.
    p = _sigmoid(_logit(oof) * cal_a[tree.depth] + cal_b[tree.depth])
    if hierarchical:
        for j in range(len(tree)):
            if tree.parent[j] >= 0:
                np.minimum(p[:, j], p[:, tree.parent[j]], out=p[:, j])
    best_t, best_f1 = 0.5, -1.0
    for t in np.arange(0.1, 0.71, 0.05):
        pred = replace(model, threshold=float(t)).decode(p.copy())
        tp = (pred & y).sum()
        f1 = 2 * tp / max(pred.sum() + y.sum(), 1)
        if f1 > best_f1 + 1e-9:
            best_t, best_f1 = float(t), float(f1)
    model = replace(model, threshold=best_t)
    exact = (model.decode(p.copy()) == y).all(axis=1)
    conf_a, conf_b = _platt(_logit(model.raw_confidence(p)), exact)
    return replace(model, conf_a=conf_a, conf_b=conf_b)
