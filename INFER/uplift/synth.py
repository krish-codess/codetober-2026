"""Synthetic experiment with a known heterogeneous effect, in the shape of the Hillstrom file and with its defects.

The columns and marginals follow the profile in docs/data-profile.md. On top of the Hillstrom columns the file
carries what a real feed would: `customer_id`, `assigned_at`, and (because we wrote the world) `true_uplift`.

The world, for the binary outcome `visit`:
  * sure things   high purchase history raises the baseline a lot and the effect not at all
  * persuadables  new web customers and recent buyers respond to the e-mail
  * sleeping dogs phone customers with history >= $350 visit LESS when e-mailed (true effect about -6 points)

Defects are injected at fixed, seeded rates and counted in a manifest, so ingestion can be checked against the
exact number of rows it must quarantine.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

COLUMNS = [
    "customer_id",
    "assigned_at",
    "recency",
    "history_segment",
    "history",
    "mens",
    "womens",
    "zip_code",
    "newbie",
    "channel",
    "segment",
    "visit",
    "conversion",
    "spend",
    "true_uplift",
]
_RECENCY_P = np.array([8952, 7537, 5904, 5077, 4510, 4605, 4077, 3495, 6441, 7565, 3504, 2333], dtype=float)
_SEGMENT_EDGES = [100, 200, 350, 500, 750, 1000]
_SEGMENT_LABELS = [
    "1) $0 - $100",
    "2) $100 - $200",
    "3) $200 - $350",
    "4) $350 - $500",
    "5) $500 - $750",
    "6) $750 - $1,000",
    "7) $1,000 +",
]
SLEEPING_DOG_HISTORY = 350.0


def true_effect(
    recency: np.ndarray, history: np.ndarray, newbie: np.ndarray, channel: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Baseline visit probability and the true uplift of being e-mailed, per unit."""
    z_hist = np.log(history) - 5.0
    multichannel = channel == "Multichannel"
    p0 = 1 / (1 + np.exp(-(-2.5 + 0.9 * z_hist - 0.07 * (recency - 6) + 0.3 * multichannel)))
    tau = (
        0.045
        + 0.06 * (newbie & (channel == "Web"))
        + 0.035 * (recency <= 3)
        - 0.11 * ((channel == "Phone") & (history >= SLEEPING_DOG_HISTORY))
    )
    p1 = np.clip(p0 + tau, 0.005, 0.995)
    return p0, p1 - p0


def generate(out_dir: Path, units: int, seed: int) -> dict[str, object]:
    """Write synthetic.csv, synthetic_late.csv and synthetic_manifest.json. Deterministic in (units, seed)."""
    rng = np.random.default_rng(seed)
    n = units
    recency = rng.choice(np.arange(1, 13), size=n, p=_RECENCY_P / _RECENCY_P.sum())
    history = np.clip(np.round(rng.lognormal(5.21, 0.85, n), 2), 30.0, 3500.0)
    history[rng.random(n) < 0.124] = 29.99  # the floor Hillstrom's file has
    gender = rng.choice(3, size=n, p=[0.45, 0.45, 0.10])
    mens, womens = (gender != 1).astype(int), (gender != 0).astype(int)
    zip_code = rng.choice(["Surburban", "Urban", "Rural"], size=n, p=[0.45, 0.40, 0.15])  # sic, as published
    newbie = rng.random(n) < 0.5
    channel = rng.choice(["Web", "Phone", "Multichannel"], size=n, p=[0.44, 0.44, 0.12])
    segment = rng.choice(["Mens E-Mail", "Womens E-Mail", "No E-Mail"], size=n)
    treated = segment != "No E-Mail"

    p0, tau = true_effect(recency, history, newbie, channel)
    visit = rng.random(n) < np.where(treated, p0 + tau, p0)
    conversion = visit & (rng.random(n) < 0.06)
    spend = np.where(conversion, np.round(rng.lognormal(4.5, 0.5, n), 2), 0.0)
    start = datetime(2026, 1, 5, tzinfo=UTC)
    assigned = np.sort(rng.integers(0, 28 * 86400, n))  # customer ids increase with time, like a real sequence

    u = rng.random((n, 12))
    rows: list[list[str]] = []
    late: list[list[str]] = []
    m = dict.fromkeys(
        ["missing_feature", "reformatted", "invalid", "late_outcome", "duplicate_row", "conflicting_duplicate"], 0
    )
    invalid_ids: set[str] = set()
    for i in range(n):
        cid = f"C{i:07d}"
        ts = start + timedelta(seconds=int(assigned[i]))
        row = {
            "customer_id": cid,
            "assigned_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recency": str(recency[i]),
            "history_segment": _SEGMENT_LABELS[int(np.searchsorted(_SEGMENT_EDGES, history[i], side="right"))],
            "history": f"{history[i]:.2f}",
            "mens": str(mens[i]),
            "womens": str(womens[i]),
            "zip_code": str(zip_code[i]),
            "newbie": str(int(newbie[i])),
            "channel": str(channel[i]),
            "segment": str(segment[i]),
            "visit": str(int(visit[i])),
            "conversion": str(int(conversion[i])),
            "spend": f"{spend[i]:.2f}",
            "true_uplift": f"{tau[i]:.6f}",
        }
        # ---- missing fields: the unit is still usable, the feature is simply unknown
        for col, j, rate in (("history", 0, 0.03), ("zip_code", 1, 0.02), ("channel", 2, 0.015), ("recency", 3, 0.01)):
            if u[i, j] < rate:
                row[col] = ""
                m["missing_feature"] += 1
        # ---- inconsistent formats: same value, different spelling
        if u[i, 4] < 0.05 and row["channel"]:
            row["channel"] = row["channel"].upper() if u[i, 4] < 0.025 else f" {row['channel'].lower()} "
            m["reformatted"] += 1
        if u[i, 5] < 0.04 and row["history"]:
            row["history"] = f"${history[i]:,.2f}"
            m["reformatted"] += 1
        if u[i, 6] < 0.03:
            row["newbie"] = "Y" if newbie[i] else "N"
            m["reformatted"] += 1
        if u[i, 7] < 0.10:
            row["assigned_at"] = ts.strftime("%m/%d/%Y %H:%M:%S")
            m["reformatted"] += 1
        # ---- values that violate the contract: must be quarantined, never loaded
        if u[i, 8] < 0.006:
            kind = int(u[i, 8] / 0.006 * 4)
            if kind == 0:
                row["recency"] = "99" if i % 2 else "-1"
            elif kind == 1:
                row["segment"] = "Test E-Mail"
            elif kind == 2:
                row["history"] = "-50.00"
            else:
                row["assigned_at"] = "not-a-date"
            m["invalid"] += 1
            invalid_ids.add(cid)
        values = [row[c] for c in COLUMNS]
        # ---- late arrivals: the outcome is not in yet; the complete row turns up in the late file
        if u[i, 9] < 0.015 and cid not in invalid_ids:
            late.append(values)
            incomplete = dict(row, visit="", conversion="", spend="")
            rows.append([incomplete[c] for c in COLUMNS])
            m["late_outcome"] += 1
            continue
        rows.append(values)
        # ---- duplicates: a resend of the same row, or the same customer recorded in a second arm
        if u[i, 10] < 0.01 and cid not in invalid_ids:
            rows.append(values)
            m["duplicate_row"] += 1
        elif u[i, 11] < 0.002 and cid not in invalid_ids:
            other = dict(row, segment="No E-Mail" if treated[i] else "Mens E-Mail")
            rows.append([other[c] for c in COLUMNS])
            m["conflicting_duplicate"] += 1

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (("synthetic.csv", rows), ("synthetic_late.csv", late)):
        with (out_dir / name).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(COLUMNS)
            writer.writerows(data)
    manifest: dict[str, object] = {
        "units": n,
        "seed": seed,
        "rows_main": len(rows),
        "rows_late": len(late),
        "mean_true_uplift": round(float(tau.mean()), 6),
        "share_negative_effect": round(float((tau < 0).mean()), 6),
        **m,
    }
    (out_dir / "synthetic_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
