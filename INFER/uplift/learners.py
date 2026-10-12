"""Uplift learners behind one interface: fit(X, t, y) and predict(X) -> a score where higher means treat first.

T- and X-learner are implemented here (they are a dozen lines each, and tests/test_differential.py holds them to
EconML's implementations to 1e-9). The causal forest is EconML's. Every learner gets the same base model, the same
features and the same rows.

Two baselines sit beside them on purpose:
  * `random`    what a campaign with no model earns
  * `response`  P(outcome | features), i.e. "target the likely buyers" - the model most teams already have, and the
                one that spends the budget on people who would have bought anyway
"""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np
import pandas as pd
from econml.dml import CausalForestDML
from numpy.typing import NDArray
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingRegressor

F = NDArray[np.float64]

NUMERIC = ["recency", "history", "mens", "womens", "newbie"]
CATEGORICAL = {"zip_code": ["Surburban", "Urban", "Rural"], "channel": ["Web", "Phone", "Multichannel"]}
FEATURES = NUMERIC + [f"{col}={v}" for col, values in CATEGORICAL.items() for v in values]


def featurize(df: pd.DataFrame, medians: dict[str, float] | None = None) -> tuple[F, dict[str, float]]:
    """Units -> model matrix. Missing numerics take the TRAINING median (pass `medians` back in at inference);
    a missing category is all-zeros across its indicators. The forest cannot take NaN, and every learner must see
    identical inputs, so imputation happens once, here."""
    num = df[NUMERIC].astype(float)
    if medians is None:
        medians = {c: float(num[c].median()) for c in NUMERIC}
    cols = [num[c].fillna(medians[c]).to_numpy() for c in NUMERIC]
    cols += [(df[col] == v).to_numpy(dtype=float) for col, values in CATEGORICAL.items() for v in values]
    return np.column_stack(cols), medians


def base_model(seed: int) -> HistGradientBoostingRegressor:
    """Shallow, heavily regularised boosting. Treatment effects are a few points on a ~10% base rate: anything
    more flexible fits noise in each arm and the difference of two noisy fits is mostly noise.
    A regressor on a 0/1 outcome estimates the same conditional mean a classifier would, and lets one model
    class serve binary and continuous outcomes and both stages of the X-learner."""
    return HistGradientBoostingRegressor(
        max_iter=120, learning_rate=0.05, max_depth=3, min_samples_leaf=200, l2_regularization=1.0, random_state=seed
    )


class Learner(Protocol):
    estimates_uplift: bool

    def __init__(self, seed: int = 0) -> None: ...
    def fit(self, X: F, t: F, y: F) -> Any: ...
    def predict(self, X: F) -> F: ...


class Random:
    estimates_uplift = False

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed

    def fit(self, X: F, t: F, y: F) -> Random:
        return self

    def predict(self, X: F) -> F:
        return np.random.default_rng(self.seed).random(len(X))


class Response:
    estimates_uplift = False

    def __init__(self, seed: int = 0) -> None:
        self.model = base_model(seed)

    def fit(self, X: F, t: F, y: F) -> Response:
        self.model.fit(X, y)
        return self

    def predict(self, X: F) -> F:
        return np.asarray(self.model.predict(X), dtype=float)


class SLearner:
    """One model with treatment as a feature; uplift = prediction with t=1 minus prediction with t=0."""

    estimates_uplift = True

    def __init__(self, seed: int = 0) -> None:
        self.model = base_model(seed)

    def fit(self, X: F, t: F, y: F) -> SLearner:
        self.model.fit(np.column_stack([X, t]), y)
        return self

    def predict(self, X: F) -> F:
        one, zero = np.ones(len(X)), np.zeros(len(X))
        return np.asarray(
            self.model.predict(np.column_stack([X, one])) - self.model.predict(np.column_stack([X, zero])), dtype=float
        )


class TLearner:
    """Two models, one per arm; uplift = mu1(x) - mu0(x)."""

    estimates_uplift = True

    def __init__(self, seed: int = 0) -> None:
        self.m0, self.m1 = base_model(seed), base_model(seed)

    def fit(self, X: F, t: F, y: F) -> TLearner:
        self.m0.fit(X[t == 0], y[t == 0])
        self.m1.fit(X[t == 1], y[t == 1])
        return self

    def predict(self, X: F) -> F:
        return np.asarray(self.m1.predict(X) - self.m0.predict(X), dtype=float)


class XLearner:
    """Kuenzel et al. (2019). Impute each unit's effect with the other arm's model, regress the imputed effects on
    features, blend by propensity: tau(x) = g * tau0(x) + (1 - g) * tau1(x). In a randomized experiment the
    propensity g is a known constant, so it is the treated share and not a fitted model."""

    estimates_uplift = True

    def __init__(self, seed: int = 0) -> None:
        self.m0, self.m1, self.tau0, self.tau1 = (base_model(seed) for _ in range(4))
        self.g = 0.5

    def fit(self, X: F, t: F, y: F) -> XLearner:
        X0, X1, y0, y1 = X[t == 0], X[t == 1], y[t == 0], y[t == 1]
        self.m0.fit(X0, y0)
        self.m1.fit(X1, y1)
        self.tau0.fit(X0, self.m1.predict(X0) - y0)
        self.tau1.fit(X1, y1 - self.m0.predict(X1))
        self.g = float(t.mean())
        return self

    def predict(self, X: F) -> F:
        return np.asarray(self.g * self.tau0.predict(X) + (1 - self.g) * self.tau1.predict(X), dtype=float)


class CausalForest:
    """EconML's CausalForestDML (generalized random forest on residualised outcome and treatment). The treatment
    model is the constant treated share, again because assignment was randomized."""

    estimates_uplift = True

    def __init__(self, seed: int = 0, n_estimators: int = 200, max_rows: int = 60_000) -> None:
        self.seed, self.max_rows = seed, max_rows
        self.model = CausalForestDML(
            model_y=base_model(seed),
            model_t=DummyClassifier(strategy="prior"),
            discrete_treatment=True,
            n_estimators=n_estimators,
            min_samples_leaf=100,
            max_depth=8,
            cv=3,
            n_jobs=2,
            random_state=seed,
        )

    def fit(self, X: F, t: F, y: F) -> CausalForest:
        if len(X) > self.max_rows:
            # ponytail: the forest is the stage that breaks first at 10x (time and memory grow with rows * trees);
            # it trains on a seeded subsample past max_rows. Raise max_rows on a bigger machine.
            keep = np.random.default_rng(self.seed).choice(len(X), self.max_rows, replace=False)
            X, t, y = X[keep], t[keep], y[keep]
        self.model.fit(y, t.astype(int), X=X)
        return self

    def predict(self, X: F) -> F:
        return np.asarray(self.model.effect(X), dtype=float).ravel()


LEARNERS: dict[str, type[Learner]] = {
    "random": Random,
    "response": Response,
    "s_learner": SLearner,
    "t_learner": TLearner,
    "x_learner": XLearner,
    "causal_forest": CausalForest,
}
