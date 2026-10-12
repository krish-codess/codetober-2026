"""Model tests: differential against EconML, a training smoke test, the inference contract, a regression gate."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from econml import metalearners
from sklearn.dummy import DummyClassifier

from uplift import metrics
from uplift.learners import FEATURES, LEARNERS, SLearner, TLearner, XLearner, base_model, featurize
from uplift.synth import true_effect

SEED = 11


def world(n: int, seed: int) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray]:
    """Clean units drawn from the synthetic world, without going through files."""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(
        {
            "recency": rng.integers(1, 13, n),
            "history": np.clip(rng.lognormal(5.21, 0.85, n), 30, 3500),
            "mens": rng.random(n) < 0.55,
            "womens": rng.random(n) < 0.55,
            "newbie": rng.random(n) < 0.5,
            "zip_code": rng.choice(["Surburban", "Urban", "Rural"], n),
            "channel": rng.choice(["Web", "Phone", "Multichannel"], n, p=[0.44, 0.44, 0.12]),
        }
    )
    p0, tau = true_effect(
        df["recency"].to_numpy(), df["history"].to_numpy(), df["newbie"].to_numpy(), df["channel"].to_numpy()
    )
    t = (rng.random(n) < 2 / 3).astype(float)
    y = (rng.random(n) < p0 + tau * t).astype(float)
    return df, t, y, tau


@pytest.fixture(scope="module")
def data() -> dict[str, np.ndarray]:
    df, t, y, tau = world(45_000, SEED)
    X, _ = featurize(df)
    cut = 30_000
    return {
        "X": X[:cut],
        "t": t[:cut],
        "y": y[:cut],
        "X_test": X[cut:],
        "t_test": t[cut:],
        "y_test": y[cut:],
        "tau_test": tau[cut:],
    }


# ---------------------------------------------------------------- differential: ours vs EconML's


def test_t_learner_matches_econml(data: dict[str, np.ndarray]) -> None:
    ours = TLearner(SEED).fit(data["X"], data["t"], data["y"]).predict(data["X_test"])
    reference = metalearners.TLearner(models=base_model(SEED))
    reference.fit(data["y"], data["t"].astype(int), X=data["X"])
    np.testing.assert_allclose(ours, reference.effect(data["X_test"]).ravel(), atol=1e-9)


def test_x_learner_matches_econml(data: dict[str, np.ndarray]) -> None:
    ours = XLearner(SEED).fit(data["X"], data["t"], data["y"]).predict(data["X_test"])
    # a constant propensity is what "randomized" means; give EconML the same
    reference = metalearners.XLearner(models=base_model(SEED), propensity_model=DummyClassifier(strategy="prior"))
    reference.fit(data["y"], data["t"].astype(int), X=data["X"])
    np.testing.assert_allclose(ours, reference.effect(data["X_test"]).ravel(), atol=1e-9)


def test_s_learner_matches_econml(data: dict[str, np.ndarray]) -> None:
    ours = SLearner(SEED).fit(data["X"], data["t"], data["y"]).predict(data["X_test"])
    reference = metalearners.SLearner(overall_model=base_model(SEED))
    reference.fit(data["y"], data["t"].astype(int), X=data["X"])
    np.testing.assert_allclose(ours, reference.effect(data["X_test"]).ravel(), atol=1e-9)


# ---------------------------------------------------------------- smoke, contract, gate


@pytest.fixture(scope="module")
def fitted(data: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {
        name: cls(seed=SEED).fit(data["X"], data["t"], data["y"]).predict(data["X_test"])
        for name, cls in LEARNERS.items()
    }


def test_every_learner_trains_and_honours_the_inference_contract(
    data: dict[str, np.ndarray], fitted: dict[str, np.ndarray]
) -> None:
    for name, scores in fitted.items():
        assert scores.shape == (len(data["X_test"]),), name
        assert scores.dtype == np.float64 and np.isfinite(scores).all(), name
        if LEARNERS[name].estimates_uplift:
            assert np.abs(scores).max() <= 1, f"{name}: an uplift on a probability cannot exceed 1"


def test_predictions_are_deterministic_and_row_independent(data: dict[str, np.ndarray]) -> None:
    a = TLearner(SEED).fit(data["X"], data["t"], data["y"])
    b = TLearner(SEED).fit(data["X"], data["t"], data["y"])
    np.testing.assert_array_equal(a.predict(data["X_test"]), b.predict(data["X_test"]))
    # scoring a unit alone gives the same answer as scoring it in a batch
    np.testing.assert_allclose(a.predict(data["X_test"][:1]), a.predict(data["X_test"])[:1])


def test_regression_gate_on_known_effects(data: dict[str, np.ndarray], fitted: dict[str, np.ndarray]) -> None:
    """Fails on a meaningful drop. The thresholds sit well under what the pinned versions achieve (see
    docs/evidence/model-report.md) and well over what a broken learner would."""
    y, t, tau = data["y_test"], data["t_test"], data["tau_test"]
    oracle = metrics.qini(y, t, tau)
    share = {name: metrics.qini(y, t, s) / oracle for name, s in fitted.items()}
    for name in ("s_learner", "t_learner", "x_learner", "causal_forest"):
        assert share[name] > 0.55, f"{name} recovers only {share[name]:.0%} of the oracle Qini"
        assert share[name] > share["response"] + 0.3, f"{name} does not clearly beat targeting likely buyers"
        pehe = float(np.sqrt(np.mean((fitted[name] - tau) ** 2)))
        constant = float(np.sqrt(np.mean((tau.mean() - tau) ** 2)))  # predicting the average effect for everyone
        assert pehe < 0.8 * constant, f"{name}: PEHE {pehe:.4f} is not better than a constant ({constant:.4f})"
    assert abs(share["random"]) < 0.25
    # the sleeping dogs: units the best learner predicts are harmed really are harmed, on average
    predicted_harm = fitted["x_learner"] < 0
    assert predicted_harm.mean() > 0.03
    assert tau[predicted_harm].mean() < -0.02


# ---------------------------------------------------------------- features


def test_featurize_uses_training_medians_and_never_emits_nan() -> None:
    train = pd.DataFrame(
        {"recency": [1, 3, 5], "history": [10.0, 20.0, 90.0], "mens": [True, False, True], "womens": [False, True, True],
         "newbie": [True, True, False], "zip_code": ["Urban", "Rural", "Urban"], "channel": ["Web", "Phone", "Web"]}
    )  # fmt: skip
    _, medians = featurize(train)
    assert medians["recency"] == 3 and medians["history"] == 20
    new = pd.DataFrame(
        {
            "recency": [None],
            "history": [None],
            "mens": [None],
            "womens": [True],
            "newbie": [None],
            "zip_code": [None],
            "channel": ["Multichannel"],
        }
    )
    X, _ = featurize(new, medians)
    row = dict(zip(FEATURES, X[0], strict=True))
    assert not np.isnan(X).any()
    assert row["recency"] == 3 and row["history"] == 20
    assert row["zip_code=Urban"] == row["zip_code=Rural"] == row["zip_code=Surburban"] == 0
    assert row["channel=Multichannel"] == 1
