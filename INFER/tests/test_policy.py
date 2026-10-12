"""The policy's one hard promise: it never spends more than the budget. Proved by property, not by example."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from uplift import policy

money = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("500"), places=2)


@given(
    st.lists(st.floats(-0.5, 0.5), min_size=0, max_size=200),
    st.decimals(min_value=0, max_value=Decimal("5000"), places=2),
    money,
    money,
)
@settings(max_examples=500, deadline=None)
def test_policy_is_within_budget_worthwhile_and_maximal(
    uplifts: list[float], budget: Decimal, cost: Decimal, value: Decimal
) -> None:
    ranked = np.sort(np.array(uplifts, dtype=float))[::-1]
    n = policy.size(ranked, budget, cost, value)
    breakeven = float(cost / value)
    assert 0 <= n <= len(ranked)
    assert n * cost <= budget  # the guarantee
    assert all(u > breakeven for u in ranked[:n])  # nobody is treated at a predicted loss
    # maximal: the next unit is either unaffordable, not worthwhile, or does not exist
    assert n == len(ranked) or (n + 1) * cost > budget or ranked[n] <= breakeven


def _world(n: int = 40_000, seed: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Half the units gain 20 points from treatment, half lose 10. Rows are ranked best-first."""
    rng = np.random.default_rng(seed)
    tau = np.where(np.arange(n) < n // 2, 0.20, -0.10)
    t = (rng.random(n) < 0.5).astype(float)
    y = (rng.random(n) < 0.30 + tau * t).astype(float)
    return y, t, tau


def test_evaluation_recovers_a_known_policy_value() -> None:
    y, t, tau = _world()
    n = len(y)
    e = policy.evaluate(y, t, n // 2, value=10.0, cost=1.0, true_uplift=tau)
    assert e["evaluable"]
    # truth per unit: policy = 10 * (0.30 + 0.5 * 0.20) - 0.5; all = 10 * 0.35 - 1; none = 3.0
    assert e["profit_per_unit"]["policy"] == pytest.approx(3.5, abs=0.08)
    assert e["profit_per_unit"]["treat_all"] == pytest.approx(2.5, abs=0.08)
    assert e["profit_per_unit"]["treat_none"] == pytest.approx(3.0, abs=0.08)
    lo, hi = e["policy_minus_treat_all"]["ci95"]
    assert (
        lo <= e["policy_minus_treat_all"]["estimate"] <= hi and lo > 0
    )  # better than blanket treatment, with evidence
    assert e["true_incremental_outcomes_per_unit"]["policy"] == pytest.approx(0.10)
    assert e["incremental_outcomes_per_unit"]["policy"] == pytest.approx(0.10, abs=0.01)
    # treating only the right half captures twice the effect of treating everyone
    assert e["share_of_treat_all_effect_captured"] == pytest.approx(2.0, abs=0.35)


def test_the_two_trivial_policies_are_the_two_blanket_policies() -> None:
    y, t, _ = _world(8000, seed=1)
    nobody = policy.evaluate(y, t, 0, 10.0, 1.0, rounds=20)
    everybody = policy.evaluate(y, t, len(y), 10.0, 1.0, rounds=20)
    assert nobody["profit_per_unit"]["policy"] == pytest.approx(nobody["profit_per_unit"]["treat_none"])
    assert everybody["profit_per_unit"]["policy"] == pytest.approx(everybody["profit_per_unit"]["treat_all"])
    assert nobody["policy_minus_treat_none"]["ci95"] == [0.0, 0.0]


def test_a_policy_too_small_to_evaluate_says_so() -> None:
    y, t, _ = _world(8000, seed=2)
    e = policy.evaluate(y, t, 10, 10.0, 1.0)
    assert e == {"evaluable": False, "reason": e["reason"]} and "fewer than" in e["reason"]


def test_a_wrong_ranking_is_exposed_by_the_experiment() -> None:
    y, t, _ = _world(seed=3)
    backwards = policy.evaluate(y[::-1].copy(), t[::-1].copy(), len(y) // 2, 10.0, 1.0)
    assert backwards["policy_minus_treat_none"]["ci95"][1] < 0  # confidently worse than doing nothing
