"""Where treatment helps and where it hurts, said in terms a marketer can act on.

Two views, deliberately independent of each other:
  * scan()      model-free. Observed uplift in every one- and two-feature segment of the whole experiment, with
                Benjamini-Hochberg correction across the family. A segment is only called 'hurts' when its adjusted
                p-value clears 5% AND its effect is negative - a negative point estimate alone is called 'unclear'.
  * profiles()  model-based. Who the model puts in its top decile and who it predicts are harmed, with the OBSERVED
                held-out uplift of each group, so a model that invents a harmed group is caught by its own interval.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from uplift.metrics import benjamini_hochberg, diff_in_means, order

F = NDArray[np.float64]
MIN_PER_ARM = 300
MIN_GROUP = 100  # a predicted-harm group smaller than this is not profiled at all
ALPHA = 0.05


def levels(df: pd.DataFrame) -> pd.DataFrame:
    """Each feature as a small set of named levels. Missing is its own level, never dropped."""

    def yes_no(col: str) -> pd.Series:
        return df[col].map({True: "yes", False: "no"}).fillna("unknown")

    recency = pd.cut(
        df["recency"].astype(float), [0, 3, 6, 9, 12], labels=["1-3 months", "4-6 months", "7-9 months", "10-12 months"]
    )
    history = pd.cut(
        df["history"].astype(float), [-1, 99.995, 349.995, np.inf], labels=["under $100", "$100-$350", "$350 and over"]
    )
    return pd.DataFrame(
        {
            "recency": recency.astype(object).fillna("unknown"),
            "history": history.astype(object).fillna("unknown"),
            "mens": yes_no("mens"),
            "womens": yes_no("womens"),
            "newbie": yes_no("newbie"),
            "zip_code": df["zip_code"].fillna("unknown"),
            "channel": df["channel"].fillna("unknown"),
        },
        index=df.index,
    )


def scan(df: pd.DataFrame) -> list[dict[str, Any]]:
    lv = levels(df)
    y, t = df["outcome"].to_numpy(float), df["treatment"].to_numpy(float)
    masks: dict[str, NDArray[np.bool_]] = {
        f"{col} = {v}": (lv[col] == v).to_numpy() for col in lv.columns for v in sorted(lv[col].unique())
    }
    candidates = dict(masks)
    for a, b in combinations(masks, 2):
        if a.split(" = ")[0] != b.split(" = ")[0]:
            candidates[f"{a} AND {b}"] = masks[a] & masks[b]
    rows: list[dict[str, Any]] = []
    for definition, mask in candidates.items():
        d = diff_in_means(y[mask], t[mask])
        if min(d["n_treated"], d["n_control"]) >= MIN_PER_ARM:
            rows.append({"definition": definition, **d})
    if not rows:
        return []
    adjusted = benjamini_hochberg(np.array([r["p"] for r in rows]))
    for r, p_adj in zip(rows, adjusted, strict=True):
        r["p_adjusted"] = float(p_adj)
        r["verdict"] = "unclear" if p_adj >= ALPHA else ("helps" if r["uplift"] > 0 else "hurts")
        del r["p"]
    return sorted(rows, key=lambda r: r["uplift"])


def _group(df: pd.DataFrame, lv: pd.DataFrame, mask: NDArray[np.bool_], score: F) -> dict[str, Any]:
    y, t = df["outcome"].to_numpy(float), df["treatment"].to_numpy(float)
    d = diff_in_means(y[mask], t[mask])
    traits = []
    for col in lv.columns:
        overall = lv[col].value_counts(normalize=True)
        inside = lv.loc[mask, col].value_counts(normalize=True)
        traits += [
            {
                "feature": col,
                "level": str(v),
                "share_in_group": float(inside.get(v, 0.0)),
                "share_overall": float(share),
            }
            for v, share in overall.items()
        ]
    # the traits that most set the group apart from everyone else, over- or under-represented
    traits.sort(key=lambda r: abs(r["share_in_group"] - r["share_overall"]), reverse=True)
    true = df["true_uplift"].to_numpy(float)[mask] if df["true_uplift"].notna().all() else None
    return {
        "n": int(mask.sum()),
        "share_of_population": float(mask.mean()),
        "mean_predicted": float(score[mask].mean()) if mask.any() else None,
        "observed_uplift": d["uplift"],
        "ci_low": d["ci_low"] if np.isfinite(d["ci_low"]) else None,
        "ci_high": d["ci_high"] if np.isfinite(d["ci_high"]) else None,
        "true_uplift": None if true is None or not mask.any() else float(true.mean()),
        # 'hurts' needs the whole interval below zero on held-out data
        "verdict": "hurts" if d["ci_high"] < 0 else "helps" if d["ci_low"] > 0 else "unclear",
        "traits": traits[:8],
    }


def profiles(df: pd.DataFrame, score: F, estimates_uplift: bool) -> dict[str, Any]:
    """Profiles of the model's top decile and of the units it predicts are harmed (held-out rows only)."""
    lv = levels(df)
    top = np.zeros(len(df), dtype=bool)
    top[order(score)[: max(1, len(df) // 10)]] = True
    out: dict[str, Any] = {"high_uplift": _group(df, lv, top, score)}
    if estimates_uplift:  # a response score has no zero, so "predicted negative" is meaningless for the baselines
        negative = score < 0
        out["negative_uplift"] = _group(df, lv, negative, score) if negative.sum() >= MIN_GROUP else None
    return out
