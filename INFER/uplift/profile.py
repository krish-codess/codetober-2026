"""Profile a raw file before trusting it: blanks, cardinality, distributions, and which rows break the contract."""

from __future__ import annotations

import csv
import statistics
from collections import Counter
from pathlib import Path

from uplift.ingest import OUTCOMES, Reject, normalize, sha256_file


def _is_number(v: str) -> bool:
    try:
        float(v)
    except ValueError:
        return False
    return True


def profile(path: Path) -> str:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        columns = list(reader.fieldnames or [])
        rows = list(reader)
    out = [
        f"## `{path.name}`\n",
        f"- SHA-256 `{sha256_file(path)}`",
        f"- {len(rows):,} rows, {len(columns)} columns",
        f"- {len(rows) - len({tuple(r.values()) for r in rows}):,} rows are exact copies of an earlier row",
        "",
        "| column | blank | distinct | values |",
        "|---|---:|---:|---|",
    ]
    for c in columns:
        values = [(r.get(c) or "").strip() for r in rows]
        present = [v for v in values if v]
        counts = Counter(present)
        if len(counts) > 12 and all(_is_number(v) for v in present):
            nums = sorted(float(v) for v in present)
            mode, times = counts.most_common(1)[0]
            shown = (
                f"min {nums[0]:g}, median {statistics.median(nums):g}, max {nums[-1]:g}; most common {mode} ({times:,})"
            )
        else:
            shown = ", ".join(f"`{v}` {n:,}" for v, n in counts.most_common(6)) + (" ..." if len(counts) > 6 else "")
        out.append(f"| {c} | {len(values) - len(present):,} | {len(counts):,} | {shown} |")

    reasons: Counter[str] = Counter()
    for r in rows:
        try:
            normalize(r, "visit")
        except Reject as rejected:
            reasons[str(rejected)] += 1
        except KeyError as missing:
            reasons[f"missing column {missing}"] += 1
    out += [
        "",
        "Rows the ingestion contract rejects (before duplicate handling): "
        + (", ".join(f"`{k}` {v:,}" for k, v in reasons.most_common()) or "none"),
        "",
    ]

    if "segment" in columns:
        out += ["| arm | units | visit rate | conversion rate | mean spend |", "|---|---:|---:|---:|---:|"]
        for arm, n in Counter(r["segment"] for r in rows).most_common():
            group = [r for r in rows if r["segment"] == arm and _is_number(r.get("visit") or "")]
            if group:
                visit, conv, spend = (sum(float(r[k]) for r in group) / len(group) for k in OUTCOMES)
                out.append(f"| {arm} | {n:,} | {visit:.4f} | {conv:.4f} | {spend:.3f} |")
            else:
                out.append(f"| {arm} | {n:,} | - | - | - |")
    return "\n".join(out) + "\n"
