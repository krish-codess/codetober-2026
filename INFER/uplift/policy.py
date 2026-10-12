"""Budget-constrained targeting policy, and what it is worth against treating everyone and treating no one.

The policy is "treat the n units with the highest predicted uplift", where n is the largest number that
  (1) the budget pays for:            n * cost_per_treatment <= budget
  (2) is predicted to pay for itself: predicted_uplift * value_per_outcome > cost_per_treatment
With one cost per unit this greedy rule is the exact optimum of the knapsack, not a heuristic.

Its value is ESTIMATED FROM THE EXPERIMENT, not from the model's own predictions: targeted units contribute the
observed outcome of targeted-and-treated units, everyone else the observed outcome of untargeted-and-control units.
Randomization makes both unbiased. A model that is wrong about who responds earns a bad number here.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import numpy as np
from numpy.typing import NDArray
from psycopg.types.json import Jsonb

F = NDArray[np.float64]
MIN_CELL = 30  # fewer evaluated units than this in a cell and the estimate is noise; say so instead


def size(uplift_desc: F, budget: Decimal, cost: Decimal, value: Decimal) -> int:
    """How many of the top-ranked units to treat. `uplift_desc` must be sorted descending."""
    affordable = int(budget // cost)
    worthwhile = int(np.searchsorted(-uplift_desc, -float(cost / value), side="left"))  # count with uplift > cost/value
    return max(0, min(affordable, worthwhile, len(uplift_desc)))


def _profits(y: F, t: F, targeted: NDArray[np.bool_], value: float, cost: float) -> tuple[float, float, float] | None:
    """Per-capita profit of (policy, treat all, treat none), or None when a needed cell is too small."""
    treated, control = t == 1, t == 0
    if treated.sum() < MIN_CELL or control.sum() < MIN_CELL:
        return None
    f = float(targeted.mean())
    inside, outside = targeted & treated, ~targeted & control
    if (f > 0 and inside.sum() < MIN_CELL) or (f < 1 and outside.sum() < MIN_CELL):
        return None
    outcome = (f * float(y[inside].mean()) if f > 0 else 0.0) + ((1 - f) * float(y[outside].mean()) if f < 1 else 0.0)
    return (
        value * outcome - cost * f,
        value * float(y[treated].mean()) - cost,
        value * float(y[control].mean()),
    )


def evaluate(
    y: F,
    t: F,
    n_targeted: int,
    value: float,
    cost: float,
    true_uplift: F | None = None,
    rounds: int = 300,
    seed: int = 0,
) -> dict[str, Any]:
    """Value of treating the first `n_targeted` rows (rows are in ranking order), per unit of population.

    Differences come with paired-bootstrap 95% intervals: the three policies are evaluated on the same resamples.
    """
    n = len(y)
    targeted = np.arange(n) < n_targeted
    point = _profits(y, t, targeted, value, cost)
    if point is None:
        return {
            "evaluable": False,
            "reason": f"fewer than {MIN_CELL} experiment units in a cell needed to estimate the policy's value",
        }
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(rounds):
        idx = rng.integers(0, n, n)
        p = _profits(y[idx], t[idx], targeted[idx], value, cost)
        if p is not None:
            draws.append(p)
    d = np.array(draws)

    def interval(x: F) -> list[float]:
        return [float(np.percentile(x, 2.5)), float(np.percentile(x, 97.5))]

    policy, treat_all, treat_none = point
    ate = float(y[t == 1].mean() - y[t == 0].mean())
    captured = (policy + cost * n_targeted / n) / value - float(y[t == 0].mean())  # incremental outcomes per capita
    out: dict[str, Any] = {
        "evaluable": True,
        "evaluated_on": n,
        "profit_per_unit": {"policy": policy, "treat_all": treat_all, "treat_none": treat_none},
        "policy_minus_treat_all": {"estimate": policy - treat_all, "ci95": interval(d[:, 0] - d[:, 1])},
        "policy_minus_treat_none": {"estimate": policy - treat_none, "ci95": interval(d[:, 0] - d[:, 2])},
        "treat_all_minus_treat_none": {"estimate": treat_all - treat_none, "ci95": interval(d[:, 1] - d[:, 2])},
        "incremental_outcomes_per_unit": {"policy": captured, "treat_all": ate},
        "share_of_population_treated": n_targeted / n,
        "share_of_treat_all_effect_captured": captured / ate if ate else None,
        "bootstrap_rounds": len(draws),
    }
    if true_uplift is not None:  # synthetic data only: the answer the estimate above should be near
        out["true_incremental_outcomes_per_unit"] = {
            "policy": float(true_uplift[targeted].sum() / n),
            "treat_all": float(true_uplift.mean()),
            "oracle_same_size": float(np.sort(true_uplift)[::-1][:n_targeted].sum() / n),
        }
    return out


class PolicyConflict(Exception):
    """The idempotency key was already used for a different request."""


class NotScored(Exception):
    """The run has no scores to build a policy from."""


_RANKED = (
    "SELECT s.unit_id, s.uplift, u.treatment, u.outcome, u.true_uplift FROM score s"
    " JOIN unit u USING (dataset_id, unit_id) WHERE s.run_id = %s AND s.split = 'test'"
    " ORDER BY s.uplift DESC, s.unit_id DESC"
)
EXPORT = (
    "SELECT unit_id, uplift FROM score WHERE run_id = %s AND split = 'test' ORDER BY uplift DESC, unit_id DESC LIMIT %s"
)


def create(conn: Any, run_id: Any, budget: Decimal, cost: Decimal, value: Decimal, key: str) -> dict[str, Any]:
    """Create a policy for a scored run, or return the one this idempotency key already created.

    The population is the held-out test split: units the model never saw, standing in for the next campaign's
    audience. The same ranking (uplift, then unit id) decides who is targeted here and who is exported.
    """
    # stored precision, so a retry compares equal to what the first attempt wrote
    budget, cost, value = (
        budget.quantize(Decimal("0.01")),
        cost.quantize(Decimal("0.0001")),
        value.quantize(Decimal("0.0001")),
    )
    select = "SELECT * FROM policy WHERE idempotency_key = %s"
    existing = conn.execute(select, [key]).fetchone()
    if existing is None:
        rows = conn.execute(_RANKED, [run_id]).fetchall()
        if not rows:
            raise NotScored("this run has no scores; run batch scoring first")
        uplift = np.array([r["uplift"] for r in rows], dtype=float)
        y = np.array([r["outcome"] for r in rows], dtype=float)
        t = np.array([r["treatment"] for r in rows], dtype=float)
        true = None if rows[0]["true_uplift"] is None else np.array([r["true_uplift"] for r in rows], dtype=float)
        n = size(uplift, budget, cost, value)
        evaluation = evaluate(y, t, n, float(value), float(cost), true)
        conn.execute(
            "INSERT INTO policy (id, run_id, idempotency_key, budget, cost_per_treatment, value_per_outcome, population,"
            " n_targeted, min_uplift, evaluation) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (idempotency_key) DO NOTHING",
            [
                uuid.uuid4(),
                run_id,
                key,
                budget,
                cost,
                value,
                len(rows),
                n,
                float(uplift[n - 1]) if n else None,
                Jsonb(evaluation),
            ],
        )
        existing = conn.execute(select, [key]).fetchone()  # ours, or the one a concurrent retry just wrote
    same = (existing["run_id"], existing["budget"], existing["cost_per_treatment"], existing["value_per_outcome"])
    if same != (run_id, budget, cost, value):
        raise PolicyConflict("this Idempotency-Key was already used with different parameters")
    return dict(existing)
