"""The evaluation is the product: if Qini is wrong, every comparison built on it is wrong.

Property tests hold the vectorised curves to a brute-force reference that follows the definition literally.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from uplift import metrics
from uplift.synth import true_effect


@st.composite
def experiments(draw: st.DrawFn) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = draw(st.integers(4, 60))
    y = np.array(draw(st.lists(st.sampled_from([0.0, 1.0, 2.5]), min_size=n, max_size=n)))
    t = np.array(draw(st.lists(st.sampled_from([0.0, 1.0]), min_size=n, max_size=n)))
    score = np.array(draw(st.lists(st.integers(-50, 50), min_size=n, max_size=n, unique=True)), dtype=float)
    return y, t, score


def reference_qini_curve(y: np.ndarray, t: np.ndarray, score: np.ndarray) -> list[float]:
    """Radcliffe's definition, one prefix at a time, no cumulative sums."""
    ranked = sorted(range(len(y)), key=lambda i: -score[i])
    n_treated = t.sum()
    out = [0.0]
    for k in range(1, len(y) + 1):
        top = ranked[:k]
        nt, nc = sum(t[i] for i in top), sum(1 - t[i] for i in top)
        yt, yc = sum(y[i] * t[i] for i in top), sum(y[i] * (1 - t[i]) for i in top)
        out.append((yt - (yc * nt / nc if nc else 0.0)) / max(n_treated, 1))
    return out


@given(experiments())
@settings(max_examples=300, deadline=None)
def test_qini_curve_matches_the_definition(data: tuple[np.ndarray, np.ndarray, np.ndarray]) -> None:
    y, t, score = data
    frac, curve = metrics.qini_curve(y, t, score)
    assert frac[0] == 0 and frac[-1] == 1
    np.testing.assert_allclose(curve, reference_qini_curve(y, t, score), atol=1e-12)


@given(experiments())
@settings(max_examples=200, deadline=None)
def test_curves_end_at_the_average_treatment_effect(data: tuple[np.ndarray, np.ndarray, np.ndarray]) -> None:
    y, t, score = data
    if t.sum() in (0, len(t)):
        return
    ate = y[t == 1].mean() - y[t == 0].mean()
    assert metrics.qini_curve(y, t, score)[1][-1] == pytest.approx(ate, abs=1e-12)
    assert metrics.uplift_curve(y, t, score)[1][-1] == pytest.approx(ate, abs=1e-12)


@given(experiments(), st.randoms(use_true_random=False))
@settings(max_examples=200, deadline=None)
def test_qini_depends_only_on_the_ranking(data: tuple[np.ndarray, np.ndarray, np.ndarray], rnd: object) -> None:
    y, t, score = data
    base = metrics.qini(y, t, score)
    # any strictly increasing transform of the score is the same ranking
    assert metrics.qini(y, t, np.exp(score / 25) * 3 - 7) == pytest.approx(base, abs=1e-12)
    # and row order is irrelevant when scores are distinct
    perm = np.array(rnd.sample(range(len(y)), len(y)))  # type: ignore[attr-defined]
    assert metrics.qini(y[perm], t[perm], score[perm]) == pytest.approx(base, abs=1e-12)


def test_a_constant_score_is_random_targeting_not_input_order() -> None:
    # Rows arrive sorted so that input order would be a perfect ranking; a model that says nothing must not get credit.
    n = 20_000
    rng = np.random.default_rng(0)
    tau = np.linspace(0.3, 0.0, n)
    t = (rng.random(n) < 0.5).astype(float)
    y = (rng.random(n) < 0.2 + tau * t).astype(float)
    assert abs(metrics.qini(y, t, np.zeros(n))) < 0.004
    assert metrics.qini(y, t, tau) > 0.02


def test_oracle_beats_everything_and_anti_oracle_is_negative_on_the_synthetic_world() -> None:
    rng = np.random.default_rng(1)
    n = 60_000
    recency = rng.integers(1, 13, n)
    history = np.clip(rng.lognormal(5.2, 0.85, n), 30, 3500)
    newbie = rng.random(n) < 0.5
    channel = rng.choice(["Web", "Phone", "Multichannel"], n, p=[0.44, 0.44, 0.12])
    p0, tau = true_effect(recency, history, newbie, channel)
    t = (rng.random(n) < 2 / 3).astype(float)
    y = (rng.random(n) < p0 + tau * t).astype(float)
    oracle, response, anti = metrics.qini(y, t, tau), metrics.qini(y, t, p0), metrics.qini(y, t, -tau)
    assert oracle > 0.008
    assert anti < -0.008
    # the true baseline probability - a perfect "who will buy" model - is a poor uplift ranking by construction
    assert response < 0.3 * oracle


def test_diff_in_means_known_values() -> None:
    y = np.array([1, 1, 1, 0, 1, 0, 0, 0], dtype=float)
    t = np.array([1, 1, 1, 1, 0, 0, 0, 0], dtype=float)
    d = metrics.diff_in_means(y, t)
    assert d["uplift"] == pytest.approx(0.5)
    se = np.sqrt(0.1875 / 3 + 0.1875 / 3)  # sample variances 0.25 each, n = 4
    assert d["ci_high"] - d["uplift"] == pytest.approx(metrics.Z95 * se)
    assert d["n_treated"] == 4 and d["n_control"] == 4
    assert metrics.diff_in_means(y[:1], t[:1])["p"] == 1.0  # nothing to compare: says so instead of dividing by zero


def test_deciles_partition_the_data_and_are_ordered_by_prediction() -> None:
    rng = np.random.default_rng(2)
    n = 5003
    score, t = rng.normal(size=n), (rng.random(n) < 0.5).astype(float)
    y = (rng.random(n) < 0.3).astype(float)
    dec = metrics.deciles(y, t, score)
    assert [d["decile"] for d in dec] == list(range(1, 11))
    assert sum(d["n_treated"] + d["n_control"] for d in dec) == n  # type: ignore[operator]
    predicted = [d["mean_predicted"] for d in dec]
    assert predicted == sorted(predicted, reverse=True)
    assert all(d["ci_low"] <= d["observed_uplift"] <= d["ci_high"] for d in dec)  # type: ignore[operator]


@given(st.lists(st.floats(0, 1), min_size=1, max_size=40))
def test_benjamini_hochberg_properties(ps: list[float]) -> None:
    p = np.array(ps)
    adj = metrics.benjamini_hochberg(p)
    assert np.all(adj >= p - 1e-15) and np.all(adj <= 1)
    order = np.argsort(p, kind="stable")
    assert np.all(np.diff(adj[order]) >= -1e-15)  # adjustment preserves the order of the evidence


def test_benjamini_hochberg_worked_example() -> None:
    adj = metrics.benjamini_hochberg(np.array([0.01, 0.04, 0.03, 0.005]))
    np.testing.assert_allclose(adj, [0.02, 0.04, 0.04, 0.02])


def test_bootstrap_is_paired_and_seeded() -> None:
    rng = np.random.default_rng(3)
    n = 3000
    t, y, s = (rng.random(n) < 0.5).astype(float), (rng.random(n) < 0.3).astype(float), rng.normal(size=n)
    a = metrics.bootstrap_qini(y, t, {"a": s, "b": s.copy()}, 20, seed=5)
    b = metrics.bootstrap_qini(y, t, {"a": s}, 20, seed=5)
    np.testing.assert_array_equal(a["a"], a["b"])  # same resamples for every model
    np.testing.assert_array_equal(a["a"], b["a"])  # and the same again for the same seed
