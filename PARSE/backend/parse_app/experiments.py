"""Offline experiments on the real corpus. Each writes reports/<name>.json; nothing here touches
the database, and every number in the README comes from these files.

    python -m parse_app.experiments all            # everything (~25 min on 8 cores)
    python -m parse_app.experiments efficiency     # one experiment

Rules the harness follows:
  * hyperparameters are chosen on a hold-out of the POOL, never on the test split;
  * thresholds and calibration come from out-of-fold predictions on labelled data only;
  * the test split is touched only to report.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer, TfidfTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

from . import metrics as M
from .active import STRATEGIES, select
from .config import get_settings
from .corpus import FEED_NOW, CachedEmbedder, Corpus, build_feed, load_corpus
from .feed import LOW_RESOURCE
from .hier import HierModel, fit, fit_calibrated
from .ingest import Rejected, validate
from .train import PARAMS, ROUTING_THRESHOLDS

REPORTS = Path(__file__).resolve().parents[2] / "reports"
SEEDS = (0, 1, 2)
# cumulative labelled-set sizes: batches of 50, then 100, then 250
SCHEDULE = [*range(100, 501, 50), *range(600, 2001, 100), *range(2250, 3001, 250)]
EVAL_AT = (100, 200, 300, 400, 500, 700, 1000, 1500, 2000, 3000)
C = float(PARAMS["c"])


class _NoEmbed:
    def embed(self, texts: list[str]) -> Any:
        raise RuntimeError(f"{len(texts)} texts are not in the embedding cache; run the pipeline's embed stage first")


@lru_cache(maxsize=1)
def corpus() -> Corpus:
    s = get_settings()
    return load_corpus(s.data_dir, CachedEmbedder(_NoEmbed(), s.data_dir / "cache" / "emb-onnx.npz"))


@lru_cache(maxsize=1)
def pool_mu() -> Any:
    return corpus().pool.x.astype(np.float64).mean(axis=0)


def save(name: str, payload: dict[str, Any]) -> None:
    REPORTS.mkdir(exist_ok=True)
    payload = {"experiment": name, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **payload}
    (REPORTS / f"{name}.json").write_text(json.dumps(payload, indent=1, default=float), encoding="utf-8")
    print(f"wrote reports/{name}.json")


def test_report(model: HierModel, by_lang: bool = True, ci: bool = False) -> dict[str, Any]:
    c = corpus()
    pred = model.decode(model.marginals(c.test.x))
    out = M.summary(c.test.y, pred, c.tree)
    if ci:
        out["hf1_ci95"] = M.bootstrap_hf1(c.test.y, pred, c.test.group)
    if by_lang:
        out["hf1_by_lang"] = {
            str(code): M.summary(c.test.y[c.test.lang == code], pred[c.test.lang == code], c.tree)["hf1"]
            for code in np.unique(c.test.lang)
        }
    return out


# --- E0: hyperparameter selection on a pool hold-out ----------------------------------------------


def hyperparams() -> None:
    c = corpus()
    rng = np.random.default_rng(0)
    perm = rng.permutation(len(c.pool))
    held, rest = perm[:2000], perm[2000:]
    rows = []
    for n in (300, 1000, 3000):
        idx = rest[:n]
        for cv in (3.0, 10.0, 30.0, 100.0, 300.0, 1000.0):
            m = fit_calibrated(c.pool.x[idx], c.pool.y[idx], c.tree, c=cv, mu=pool_mu())
            pred = m.decode(m.marginals(c.pool.x[held]))
            rows.append(
                {
                    "n": n,
                    "c": cv,
                    "holdout_hf1": M.summary(c.pool.y[held], pred, c.tree)["hf1"],
                    "threshold": m.threshold,
                }
            )
            print(rows[-1])
    mean = {
        cv: float(np.mean([r["holdout_hf1"] for r in rows if r["c"] == cv])) for cv in sorted({r["c"] for r in rows})
    }
    save("hyperparams", {"selected_on": "2000-item hold-out of the pool (test split untouched)", "rows": rows,
                         "mean_hf1_by_c": mean, "best_c": max(mean, key=mean.__getitem__), "c_in_use": C})  # fmt: skip


# --- E1: baselines ---------------------------------------------------------------------------------


def _tfidf_flat(idx: np.ndarray) -> dict[str, Any]:
    """No embeddings, no hierarchy: character n-gram TF-IDF + one logistic regression per node."""
    c = corpus()
    vec = make_pipeline(HashingVectorizer(analyzer="char_wb", ngram_range=(2, 4), n_features=2**18, alternate_sign=False),
                        TfidfTransformer())  # fmt: skip
    xtr = vec.fit_transform([c.pool.text[i] for i in idx])
    xte = vec.transform(c.test.text)
    ytr = c.pool.y[idx]
    pred = np.zeros(c.test.y.shape, dtype=bool)
    for j in range(len(c.tree)):
        if 0 < ytr[:, j].sum() < len(idx):
            pred[:, j] = LogisticRegression(C=10.0, max_iter=200).fit(xtr, ytr[:, j]).predict(xte)
    out = M.summary(c.test.y, pred, c.tree)
    out["hf1_ci95"] = M.bootstrap_hf1(c.test.y, pred, c.test.group)
    out["hf1_by_lang"] = {str(code): M.summary(c.test.y[c.test.lang == code], pred[c.test.lang == code], c.tree)["hf1"]
                          for code in np.unique(c.test.lang)}  # fmt: skip
    return out


def baselines() -> None:
    c = corpus()
    rng = np.random.default_rng(0)
    out: dict[str, Any] = {"n_pool": len(c.pool), "n_test": len(c.test), "n_nodes": len(c.tree), "settings": {}}
    for label, idx in (
        ("n=500 (random)", rng.choice(len(c.pool), 500, replace=False)),
        ("full pool", np.arange(len(c.pool))),
    ):
        x, y = c.pool.x[idx], c.pool.y[idx]
        # Trivial heuristic: predict every node that is positive in more than half of the training labels.
        prior = np.tile(y.mean(axis=0) >= 0.5, (len(c.test), 1))
        s = M.summary(c.test.y, prior, c.tree)
        s["hf1_ci95"] = M.bootstrap_hf1(c.test.y, prior, c.test.group)
        # ...and the strongest trivial one: always predict the single most frequent root.
        top = np.zeros(len(c.tree), dtype=bool)
        top[np.argmax(y.mean(axis=0))] = True
        s2 = M.summary(c.test.y, np.tile(top, (len(c.test), 1)), c.tree)
        rows = {
            "majority (nodes positive in >50% of labels)": s,
            "most frequent root only": s2,
            "tfidf char n-grams, flat": _tfidf_flat(idx),
            "embeddings, flat (no hierarchy)": test_report(
                fit_calibrated(x, y, c.tree, c=C, mu=pool_mu(), hierarchical=False), ci=True
            ),
            "embeddings, hierarchical (this system)": test_report(
                fit_calibrated(x, y, c.tree, c=C, mu=pool_mu()), ci=True
            ),
        }
        out["settings"][label] = rows
        for name, r in rows.items():
            print(
                f"{label:16s} {name:44s} hF1={r['hf1']:.3f} exact={r['exact_match']:.3f} consistent={r['consistent']:.4f}"
            )
    save("baselines", out)


# --- E2: label efficiency ----------------------------------------------------------------------------


def _al_run(args: tuple[str, int]) -> dict[str, Any]:
    strategy, seed = args
    c = corpus()
    n = len(c.pool)
    labelled = np.zeros(n, dtype=bool)
    labelled[np.random.default_rng(seed).choice(n, SCHEDULE[0], replace=False)] = True  # same start for every strategy
    rng = np.random.default_rng(1000 + seed)
    model: HierModel | None = None
    points = []
    t0 = time.perf_counter()
    for target in SCHEDULE:
        need = target - int(labelled.sum())
        if need > 0:
            labelled[select(strategy, model, c.pool.x, np.flatnonzero(~labelled), need, rng)] = True
        idx = np.flatnonzero(labelled)
        if target in EVAL_AT:
            model = fit_calibrated(c.pool.x[idx], c.pool.y[idx], c.tree, c=C, mu=pool_mu())
            r = test_report(model)
            points.append({
                "n_labeled": target, "hf1": r["hf1"], "macro_f1": r["macro_f1"], "exact_match": r["exact_match"],
                "f1_depth1": r["f1_depth1"], "f1_depth2": r["f1_depth2"], "f1_depth3": r["f1_depth3"],
                "hf1_by_lang": r["hf1_by_lang"], "nodes_with_labels": int(c.pool.y[idx].any(axis=0).sum()),
                "lang_mix": {str(k): int(v) for k, v in zip(*np.unique(c.pool.lang[idx], return_counts=True), strict=True)},
            })  # fmt: skip
        else:
            model = fit(c.pool.x[idx], c.pool.y[idx], c.tree, c=C, mu=pool_mu())
    return {"strategy": strategy, "seed": seed, "points": points, "seconds": round(time.perf_counter() - t0, 1)}


def _labels_to_reach(ns: list[int], means: list[float], target: float) -> float | None:
    """Linear interpolation of the mean curve; None if the target is not reached within the budget."""
    for i, v in enumerate(means):
        if v >= target:
            if i == 0:
                return float(ns[0])
            lo, hi = means[i - 1], v
            return float(ns[i - 1] + (ns[i] - ns[i - 1]) * (target - lo) / (hi - lo))
    return None


def efficiency() -> None:
    c = corpus()
    full = test_report(fit_calibrated(c.pool.x, c.pool.y, c.tree, c=C, mu=pool_mu()), ci=True)
    jobs = [(s, seed) for s in STRATEGIES for seed in SEEDS]
    with ProcessPoolExecutor(max_workers=min(len(jobs), 6)) as pool:
        runs = list(pool.map(_al_run, jobs))
    curves: dict[str, Any] = {}
    for s in STRATEGIES:
        mine = [r for r in runs if r["strategy"] == s]
        curve = []
        for i, n in enumerate(EVAL_AT):
            vals = [r["points"][i]["hf1"] for r in mine]
            curve.append({"n_labeled": n, "hf1_mean": float(np.mean(vals)), "hf1_sd": float(np.std(vals)),
                          "macro_f1_mean": float(np.mean([r["points"][i]["macro_f1"] for r in mine])),
                          "nodes_with_labels_mean": float(np.mean([r["points"][i]["nodes_with_labels"] for r in mine]))})  # fmt: skip
        curves[s] = curve
    targets = {}
    for frac in (0.80, 0.85, 0.90, 0.95):
        goal = frac * full["hf1"]
        targets[f"{int(frac * 100)}%"] = {
            "target_hf1": goal,
            **{s: _labels_to_reach(list(EVAL_AT), [p["hf1_mean"] for p in curves[s]], goal) for s in STRATEGIES},
        }
    save("label_efficiency", {
        "pool_size": len(c.pool), "test_size": len(c.test), "n_nodes": len(c.tree), "seeds": list(SEEDS),
        "full_pool": {"n_labeled": len(c.pool), "hf1": full["hf1"], "hf1_ci95": full["hf1_ci95"], "macro_f1": full["macro_f1"]},
        "curves": curves, "labels_to_reach_fraction_of_full": targets, "runs": runs,
    })  # fmt: skip
    for s in STRATEGIES:
        print(s, [(p["n_labeled"], round(p["hf1_mean"], 3)) for p in curves[s]])
    print("full", round(full["hf1"], 3), json.dumps(targets, default=float))


# --- E3: cross-lingual transfer ----------------------------------------------------------------------


def crosslingual() -> None:
    c = corpus()
    rng = np.random.default_rng(0)
    en = np.flatnonzero(c.pool.lang == "en")
    out: dict[str, Any] = {
        "pool_lang_counts": {str(k): int(v) for k, v in zip(*np.unique(c.pool.lang, return_counts=True), strict=True)}
    }

    # (a) zero-shot: train on English only, test on every language
    zero = test_report(fit_calibrated(c.pool.x[en], c.pool.y[en], c.tree, c=C, mu=pool_mu()))
    mixed_idx = rng.choice(len(c.pool), len(en), replace=False)  # same label budget, natural language mix
    mixed = test_report(fit_calibrated(c.pool.x[mixed_idx], c.pool.y[mixed_idx], c.tree, c=C, mu=pool_mu()))
    out["zero_shot"] = {
        "n_train": len(en),
        "english_only": zero["hf1_by_lang"],
        "mixed_same_budget": mixed["hf1_by_lang"],
    }

    # (b) few-shot in the low-resource tail: English model + k labels in the target language,
    #     against a model that only ever saw those k target-language labels.
    few: dict[str, Any] = {}
    for lang in LOW_RESOURCE:
        own = np.flatnonzero(c.pool.lang == lang)
        rng.shuffle(own)
        mask = c.test.lang == lang
        rows = []
        for k in (0, 25, 50, 100, len(own)):
            if k > len(own):
                continue
            idx = np.concatenate([en, own[:k]])
            m = fit_calibrated(c.pool.x[idx], c.pool.y[idx], c.tree, c=C, mu=pool_mu())
            transfer = M.summary(c.test.y[mask], m.decode(m.marginals(c.test.x[mask])), c.tree)["hf1"]
            alone = None
            if k >= 25:
                m2 = fit_calibrated(c.pool.x[own[:k]], c.pool.y[own[:k]], c.tree, c=C, mu=pool_mu())
                alone = M.summary(c.test.y[mask], m2.decode(m2.marginals(c.test.x[mask])), c.tree)["hf1"]
            rows.append({"k_target_labels": k, "english_plus_k": transfer, "k_alone": alone})
        few[lang] = {"available_in_pool": len(own), "rows": rows}
        print(lang, rows)
    out["few_shot"] = few
    out["english_test_reference"] = zero["hf1_by_lang"]["en"]
    save("crosslingual", out)


# --- E4: taxonomy change -----------------------------------------------------------------------------


def taxonomy_change(n_labels: int = 1500, split_path: str = "hotel/rooms") -> None:
    """v1 lacks the children of `split_path`; n_labels items are labelled under v1. Then the node
    is split into its real children (v2). Compare what each relabelling policy costs and delivers."""
    c = corpus()
    tree = c.tree
    parent_pos = c.paths.index(split_path)
    children = np.flatnonzero(tree.parent == parent_pos)
    rng = np.random.default_rng(0)
    idx = rng.choice(len(c.pool), n_labels, replace=False)
    x, y_v2 = c.pool.x[idx], c.pool.y[idx]
    y_v1 = y_v2.copy()
    y_v1[:, children] = False  # what annotators could express before the split

    # Targeted: only items whose most specific v1 label is the split node get looked at again.
    affected = y_v1[:, parent_pos]
    y_targeted = y_v1.copy()
    y_targeted[np.ix_(affected, children)] = y_v2[np.ix_(affected, children)]
    assert (y_targeted == y_v2).all(), "targeted relabel must reproduce a full relabel exactly"

    fresh = rng.choice(np.setdiff1d(np.arange(len(c.pool)), idx), int(affected.sum()), replace=False)

    def score(xs: Any, ys: Any) -> dict[str, float]:
        m = fit_calibrated(xs, ys, tree, c=C, mu=pool_mu())
        pred = m.decode(m.marginals(c.test.x))
        tp, fp, fn = M.node_counts(c.test.y, pred)
        return {"hf1": M.summary(c.test.y, pred, tree)["hf1"],
                "f1_new_children": M.prf(tp[children].sum(), fp[children].sum(), fn[children].sum())[2]}  # fmt: skip

    policies = {
        "keep old labels, relabel nothing": {"relabelled": 0, "labels_kept": n_labels, **score(x, y_v1)},
        "targeted relabel (this system)": {"relabelled": int(affected.sum()), "labels_kept": n_labels, **score(x, y_targeted)},
        "relabel everything": {"relabelled": n_labels, "labels_kept": n_labels, **score(x, y_v2)},
        "discard labels, spend the targeted budget on new items": {
            "relabelled": int(affected.sum()), "labels_kept": 0, **score(c.pool.x[fresh], c.pool.y[fresh])},
    }  # fmt: skip
    for k, v in policies.items():
        print(f"{k:58s} {v}")
    save("taxonomy_change", {
        "split_node": split_path, "new_children": [c.paths[j] for j in children], "n_labels_before_change": n_labels,
        "items_flagged_for_review": int(affected.sum()), "targeted_equals_full_relabel": True, "policies": policies,
    })  # fmt: skip


# --- E5 + E6: calibration and per-node performance ---------------------------------------------------


def calibration_and_nodes(n: int = 1000) -> None:
    c = corpus()
    rng = np.random.default_rng(0)
    out: dict[str, Any] = {}
    for label, idx in ((f"n={n}", rng.choice(len(c.pool), n, replace=False)), ("full pool", np.arange(len(c.pool)))):
        m = fit_calibrated(c.pool.x[idx], c.pool.y[idx], c.tree, c=C, mu=pool_mu())
        p_raw, p = m.marginals(c.test.x, calibrated=False), m.marginals(c.test.x)
        pred = m.decode(p.copy())
        exact = (pred == c.test.y).all(axis=1)
        conf, raw = m.confidence(p), m.raw_confidence(p)
        live = p_raw >= 0.01  # node-level: ignore the ocean of trivially-zero (item, node) pairs
        routing: list[dict[str, Any]] = []
        for t in ROUTING_THRESHOLDS:
            sel = conf >= t
            lo, hi = M.wilson(int(exact[sel].sum()), int(sel.sum()))
            routing.append({"threshold": t, "coverage": float(sel.mean()), "n": int(sel.sum()),
                            "exact_match": float(exact[sel].mean()) if sel.any() else None, "ci95": [lo, hi]})  # fmt: skip
        tp, fp, fn = M.node_counts(c.test.y, pred)
        nodes: list[dict[str, Any]] = [{"path": c.paths[j], "depth": int(c.tree.depth[j]), "support": int(tp[j] + fn[j]), "predicted": int(tp[j] + fp[j]),
                  "precision": M.prf(tp[j], fp[j], fn[j])[0], "recall": M.prf(tp[j], fp[j], fn[j])[1], "f1": M.prf(tp[j], fp[j], fn[j])[2],
                  "train_support": int(c.pool.y[idx][:, j].sum())} for j in range(len(c.tree))]  # fmt: skip
        out[label] = {
            "threshold": m.threshold,
            "item_confidence": {"ece_calibrated": M.ece(conf, exact), "ece_uncalibrated": M.ece(raw, exact),
                                "reliability": M.reliability(conf, exact), "routing": routing},
            "node_probability": {"ece_calibrated": M.ece(p[live], c.test.y[live]), "ece_uncalibrated": M.ece(p_raw[live], c.test.y[live]),
                                 "pairs": int(live.sum())},
            "nodes": nodes,
            "weakest_supported_nodes": sorted((nd for nd in nodes if nd["support"] >= 200), key=lambda nd: nd["f1"])[:15],
        }  # fmt: skip
        print(label, "item ECE", round(out[label]["item_confidence"]["ece_calibrated"], 3), "uncal",
              round(out[label]["item_confidence"]["ece_uncalibrated"], 3), "routing", [(r["threshold"], round(r["coverage"], 3), r["exact_match"] and round(r["exact_match"], 3)) for r in routing])  # fmt: skip
    save("calibration_and_nodes", out)


# --- E7: leakage audit + data profile + throughput ---------------------------------------------------


def audit() -> None:
    s = get_settings()
    c = corpus()
    feed = build_feed(s.data_dir, "real", sorted(set(c.test.lang.tolist())))
    reasons: dict[str, int] = {}
    lines = 0
    with feed.open("rb") as f:
        for line in f:
            lines += 1
            r = validate(line.rstrip(b"\r\n"), "feed", FEED_NOW)
            if isinstance(r, Rejected):
                reasons[r.reason] = reasons.get(r.reason, 0) + 1
    pool_groups, test_groups = set(c.pool.group.tolist()), set(c.test.group.tolist())
    timings = {}
    for n in (300, 1000, 3000, len(c.pool)):
        idx = np.arange(n)
        t = time.perf_counter()
        fit(c.pool.x[idx], c.pool.y[idx], c.tree, c=C, mu=pool_mu())
        plain = time.perf_counter() - t
        t = time.perf_counter()
        m = fit_calibrated(c.pool.x[idx], c.pool.y[idx], c.tree, c=C, mu=pool_mu())
        timings[str(n)] = {"fit_s": round(plain, 2), "fit_calibrated_s": round(time.perf_counter() - t, 2)}
    t = time.perf_counter()
    m.decode(m.marginals(c.test.x[:10000]))
    infer = time.perf_counter() - t
    t = time.perf_counter()
    m.entropy(c.pool.x)
    ent = time.perf_counter() - t
    save("audit", {
        "feed": {"lines": lines, "quarantined": reasons, "pool_after_dedupe": len(c.pool), "test": len(c.test)},
        "leakage": {
            "groups_in_both_splits": len(pool_groups & test_groups),
            "exact_texts_in_both_splits_after_guard": len(set(c.pool.text) & set(c.test.text)),
            "duplicate_texts_within_pool": len(c.pool.text) - len(set(c.pool.text)),
            "features_available_at_prediction_time": "text only -> embedding; no label-derived or post-hoc field is used",
            "unsupervised_statistics_from_unlabelled_pool": ["feature mean (mu)"],
            "threshold_and_calibration_source": "out-of-fold predictions on labelled pool items",
            "hyperparameter_source": "pool hold-out (reports/hyperparams.json)",
        },
        "throughput": {
            "train_seconds_by_n_labels": timings,
            "classifier_inference_items_per_s": round(10000 / infer),
            "pool_entropy_scoring_items_per_s": round(len(c.pool) / ent),
            "note": "embedding throughput is measured by the pipeline (docs/PERFORMANCE.md)",
        },
        "labels": {"mean_nodes_per_item": float(c.pool.y.sum(1).mean()), "nodes": len(c.tree),
                   "nodes_by_depth": {str(d): int((c.tree.depth == d).sum()) for d in np.unique(c.tree.depth)},
                   "median_pool_support_per_node": int(np.median(c.pool.y.sum(0)))},
    })  # fmt: skip


EXPERIMENTS = {
    "hyperparams": hyperparams, "baselines": baselines, "efficiency": efficiency, "crosslingual": crosslingual,
    "taxonomy_change": taxonomy_change, "calibration_and_nodes": calibration_and_nodes, "audit": audit,
}  # fmt: skip

if __name__ == "__main__":
    names = sys.argv[1:] or ["all"]
    for name in EXPERIMENTS if names == ["all"] else names:
        start = time.perf_counter()
        EXPERIMENTS[name]()
        print(f"[{name}] {time.perf_counter() - start:.0f}s")
