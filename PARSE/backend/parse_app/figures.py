"""Render the committed figures in docs/figures from reports/*.json.

    pip install -e ".[experiments]" && python -m parse_app.figures

Colours are the first slots of a categorical palette validated for colour-vision deficiency;
every series is also directly labelled, and the numbers are in the README tables.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "figures"
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e2dc", "#fcfcfb"
SERIES = {  # fixed entity -> colour, never by rank
    "mixed": ("Mixed (default)", "#2a78d6", "-"),
    "random": ("Random", "#eb6834", "-"),
    "least_confident": ("Least confident", "#1baf7a", "-"),
    "entropy_sum": ("Summed entropy (failed)", "#eda100", "--"),
}


def load(name: str) -> dict[str, Any]:
    return dict(json.loads((ROOT / "reports" / f"{name}.json").read_text(encoding="utf-8")))


def style(ax: Any, xlabel: str, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.set_xlabel(xlabel, color=MUTED, fontsize=9)
    ax.set_ylabel(ylabel, color=MUTED, fontsize=9)


def efficiency() -> None:
    d = load("label_efficiency")
    fig, ax = plt.subplots(figsize=(8, 4.2), facecolor=SURFACE)
    style(ax, "labelled items", "held-out hierarchical F1")
    ends = []
    for key, (label, colour, dash) in SERIES.items():
        curve = d["curves"][key]
        xs = [p["n_labeled"] for p in curve]
        ys = [p["hf1_mean"] for p in curve]
        sd = [p["hf1_sd"] for p in curve]
        ax.fill_between(xs, [y - s for y, s in zip(ys, sd, strict=True)], [y + s for y, s in zip(ys, sd, strict=True)],
                        color=colour, alpha=0.12, linewidth=0)  # fmt: skip
        ax.plot(xs, ys, color=colour, linewidth=2, linestyle=dash, marker="o", markersize=4)
        ends.append((ys[-1], label, colour))
    full = d["full_pool"]
    ax.axhline(full["hf1"], color=MUTED, linewidth=1, linestyle=(0, (4, 4)))
    ax.text(
        40,
        full["hf1"] + 0.006,
        f"all {full['n_labeled']:,} pool items labelled: {full['hf1']:.3f}",
        color=MUTED,
        fontsize=9,
    )
    ends.sort(reverse=True)
    y_prev = 1.0
    for y, label, _colour in ends:  # direct labels, nudged apart; ink text, the line carries the colour
        y_text = min(y, y_prev - 0.016)
        ax.scatter([3070], [y_text], color=_colour, s=22, clip_on=False, zorder=3)
        ax.text(3130, y_text, label, color=INK, fontsize=9, va="center")
        y_prev = y_text
    ax.set_xlim(0, 3900)
    ax.set_title("Label efficiency on the real corpus (mean ± sd of 3 seeds)", color=INK, fontsize=11, loc="left")
    fig.tight_layout()
    fig.savefig(OUT / "label_efficiency.png", dpi=150)
    plt.close(fig)


def languages() -> None:
    b = load("baselines")["settings"]["full pool"]
    x = load("crosslingual")["zero_shot"]
    rows = sorted(b["embeddings, hierarchical (this system)"]["hf1_by_lang"].items(), key=lambda kv: -kv[1])
    langs = [k for k, _ in rows]
    fig, ax = plt.subplots(figsize=(8, 4.2), facecolor=SURFACE)
    style(ax, "test language (same sentences in every language)", "held-out hierarchical F1")
    groups = [
        ("Trained on the mixed-language pool", [v for _, v in rows], "#2a78d6"),
        ("Trained on English only (zero-shot)", [x["english_only"][k] for k in langs], "#eb6834"),
        (
            "Character n-gram TF-IDF baseline",
            [b["tfidf char n-grams, flat"]["hf1_by_lang"][k] for k in langs],
            "#1baf7a",
        ),
    ]
    width = 0.27
    for i, (label, values, colour) in enumerate(groups):
        ax.bar([j + (i - 1) * width for j in range(len(langs))], values, width - 0.03, color=colour, label=label)
    ax.set_xticks(range(len(langs)), langs)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK, loc="upper right")
    ax.set_title("Cross-lingual transfer", color=INK, fontsize=11, loc="left")
    fig.tight_layout()
    fig.savefig(OUT / "languages.png", dpi=150)
    plt.close(fig)


def calibration() -> None:
    """Reliability diagrams: of everything predicted with probability ~p, what share was right?"""
    c = load("calibration_and_nodes")["full pool"]["routing"]
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.9), facecolor=SURFACE)
    panels = [
        ("Individual labels", c["label_reliability"], c["ece_label"]),
        ("Top-level domain of an item", c["domain_reliability"], c["ece_domain"]),
    ]
    for ax, (title, bins, err) in zip(axes, panels, strict=True):
        style(ax, "predicted probability (bin mean)", "observed share correct")
        ax.plot([0, 1], [0, 1], color=MUTED, linewidth=1, linestyle=(0, (4, 4)))
        ax.plot([b["confidence"] for b in bins], [b["accuracy"] for b in bins], color="#2a78d6", linewidth=2,
                marker="o", markersize=6)  # fmt: skip
        ax.text(
            0.03, 0.97, f"calibration error {err:.3f}\ndashed: perfectly calibrated", color=MUTED, fontsize=8, va="top"
        )
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_title(title, color=INK, fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(OUT / "calibration.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for fn in (efficiency, languages, calibration):
        fn()
        print("wrote", fn.__name__)
