"""Train -> evaluate -> gate -> promote -> rescore the pool. One function, used by the scheduled
worker, the admin-triggered job and the seed."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

import numpy as np
from sqlalchemy import Engine, text

from . import metrics as M
from .active import mixed_order
from .config import Settings
from .db import bulk_insert
from .hier import HierModel, fit_calibrated
from .log import event, metrics
from .store import dumps, labelled_sets, pool_mean, set_progress, unlabelled_pool
from .taxonomy import Bools, Tree, current_version, load_tree

logger = logging.getLogger(__name__)

MIN_LABELS = 30
PARAMS = {"c": 30.0, "folds": 4, "seed": 0}  # c chosen on a pool hold-out: reports/hyperparams.json
QUEUE_HEAD = 200  # how many queue slots get the mixed (uncertain + random) ordering
CANDIDATE_FLOOR = 0.05  # nodes below this probability are not stored as suggestions
MAX_CANDIDATES = 12
ROUTING_THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)


class NotEnoughLabels(Exception):
    pass


def evaluate(model: HierModel, x: Any, y: Bools, lang: list[str], group: list[str]) -> tuple[dict[str, Any], Bools]:
    """Everything the UI and the gate need, computed once on the held-out split."""
    p = model.marginals(x)
    pred = model.decode(p.copy())
    tree = model.tree
    out = M.summary(y, pred, tree)
    out["hf1_ci95"] = M.bootstrap_hf1(y, pred, np.array(group)) if len(y) else (0.0, 0.0)

    langs = np.array(lang)
    out["by_lang"] = {str(code): M.summary(y[langs == code], pred[langs == code], tree) for code in np.unique(langs)}

    # Calibration, measured where it is used: is "confidence 0.9" right 90% of the time?
    conf = model.confidence(p)
    exact = (pred == y).all(axis=1)
    raw = model.raw_confidence(p)
    out["calibration"] = {
        "ece_item": M.ece(conf, exact), "ece_item_uncalibrated": M.ece(raw, exact),
        "reliability": M.reliability(conf, exact),
        "routing": [
            {"threshold": t, "coverage": float((conf >= t).mean()),
             "exact_match": float(exact[conf >= t].mean()) if (conf >= t).any() else None,
             "n": int((conf >= t).sum())}
            for t in ROUTING_THRESHOLDS
        ],
        **M.routing_tables(p, y, tree.depth == 1, ROUTING_THRESHOLDS),
    }  # fmt: skip
    return out, pred


def train(engine: Engine, settings: Settings, job_id: int | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    set_progress(engine, job_id, 0.05, "loading labels")
    with engine.connect() as conn:
        tree = load_tree(conn)
        version = current_version(conn)
        ids, x, y, _, _ = labelled_sets(conn, "pool", tree)
        if len(ids) < MIN_LABELS:
            raise NotEnoughLabels(f"{len(ids)} labelled items; need at least {MIN_LABELS} to train")
        mu = pool_mean(conn)
        test_ids, xt, yt, lang_t, group_t = labelled_sets(conn, "test", tree)
        active = conn.execute(
            text("SELECT id, taxonomy_version, metrics->>'hf1' AS hf1 FROM model_versions WHERE status = 'active'")
        ).one_or_none()

    load_s = time.perf_counter() - t0
    set_progress(engine, job_id, 0.15, f"fitting {len(tree)} node classifiers on {len(ids)} items")
    model = fit_calibrated(x, y, tree, mu=mu, **PARAMS)  # type: ignore[arg-type]
    fit_s = time.perf_counter() - t0 - load_s

    set_progress(engine, job_id, 0.55, f"evaluating on {len(test_ids)} held-out items")
    result, pred = evaluate(model, xt, yt, lang_t, group_t)
    result.update(
        load_seconds=round(load_s, 2),
        fit_seconds=round(fit_s, 2),
        eval_seconds=round(time.perf_counter() - t0 - load_s - fit_s, 2),
        nodes=len(tree),
        nodes_trained=int(model.trained.sum()),
        threshold=model.threshold,
    )

    # Regression gate. A candidate that is meaningfully worse than what is serving is kept for
    # inspection but never promoted.
    hf1 = float(result["hf1"])
    if active is None:
        gate = {"passed": True, "reason": "first model"}
    elif active.taxonomy_version != version:
        gate = {"passed": True, "reason": "taxonomy changed since the active model; scores are not comparable",
                "baseline_model": active.id}  # fmt: skip
    else:
        base = float(active.hf1)
        passed = hf1 >= base - settings.gate_max_drop
        gate = {"passed": passed, "baseline_model": active.id, "baseline_hf1": base, "candidate_hf1": hf1,
                "max_drop": settings.gate_max_drop,
                "reason": "ok" if passed else f"hF1 fell {base - hf1:.3f} (> {settings.gate_max_drop})"}  # fmt: skip

    artifact = model.to_bytes()
    data_hash = hashlib.sha256(
        f"{version}|{settings.embed_model_name}|".encode()
        + np.array(ids, dtype="<i8").tobytes()
        + np.packbits(y).tobytes()
    ).hexdigest()
    set_progress(engine, job_id, 0.7, "saving model version")
    with engine.begin() as conn:
        if gate["passed"]:
            conn.execute(text("UPDATE model_versions SET status = 'archived' WHERE status = 'active'"))
        model_id = conn.execute(
            text(
                """INSERT INTO model_versions (status, taxonomy_version, n_labeled, train_data_sha256, code_version,
                       embed_model, params, metrics, gate, artifact, artifact_sha256)
                   VALUES (:status, :tv, :n, :dh, :code, :em, CAST(:params AS jsonb), CAST(:metrics AS jsonb),
                       CAST(:gate AS jsonb), :artifact, :ah) RETURNING id"""
            ),
            {
                "status": "active" if gate["passed"] else "rejected",
                "tv": version,
                "n": len(ids),
                "dh": data_hash,
                "code": os.environ.get("GIT_SHA", "dev"),
                "em": settings.embed_model_name,
                "params": dumps(PARAMS),
                "metrics": dumps(result),
                "gate": dumps(gate),
                "artifact": artifact,
                "ah": hashlib.sha256(artifact).hexdigest(),
            },  # fmt: skip
        ).scalar_one()
        _write_node_metrics(conn, model_id, tree, yt, pred, lang_t)

    scored = 0
    if gate["passed"]:
        set_progress(engine, job_id, 0.8, "rescoring the unlabelled pool")
        scored = score_pool(engine, model_id, model)
    metrics.inc("train_runs_total", outcome="promoted" if gate["passed"] else "rejected")
    metrics.observe("train", time.perf_counter() - t0)
    out = {"model_version": model_id, "promoted": gate["passed"], "gate": gate, "n_labeled": len(ids), "hf1": hf1,
           "n_test": len(test_ids), "scored": scored, "seconds": round(time.perf_counter() - t0, 2)}  # fmt: skip
    event(logger, "train_done", **out)
    return out


def _write_node_metrics(conn: Any, model_id: int, tree: Tree, y: Bools, pred: Bools, lang: list[str]) -> None:
    langs = np.array(lang)
    rows = []
    for code in ["all", *np.unique(langs)]:
        m = np.ones(len(y), dtype=bool) if code == "all" else langs == code
        tp, fp, fn = M.node_counts(y[m], pred[m])
        rows += [
            {"model_version_id": model_id, "node_id": int(tree.node_ids[j]), "lang": str(code),
             "tp": int(tp[j]), "fp": int(fp[j]), "fn": int(fn[j])}
            for j in range(len(tree))
            if tp[j] or fp[j] or fn[j]
        ]  # fmt: skip
    cols = {"model_version_id": "int", "node_id": "int", "lang": "text", "tp": "int", "fp": "int", "fn": "int"}
    bulk_insert(conn, "node_metrics", cols, rows)


def prediction_rows(model: HierModel, model_id: int, ids: list[int], x: Any) -> list[dict[str, Any]]:
    p = model.marginals(x)
    conf = model.confidence(p)
    ent = model.uncertainty(x)
    # Queue order ("mixed" strategy): the first QUEUE_HEAD slots interleave one-per-cluster picks
    # among the most uncertain items with uniformly random ones (priority in (1, 2]); everything
    # else falls back to plain uncertainty (priority in [0, 1]).
    priority = ent.copy()
    for rank, pos in enumerate(mixed_order(x, ent, QUEUE_HEAD, np.random.default_rng(model_id))):
        priority[pos] = 2.0 - rank / QUEUE_HEAD
    rows = []
    for i, fid in enumerate(ids):
        top = np.argsort(-p[i], kind="stable")[:MAX_CANDIDATES]
        top = top[p[i, top] >= CANDIDATE_FLOOR]
        rows.append(
            # array-valued columns travel as array literals inside a text[] (see score_pool)
            {"f": fid, "m": model_id, "nodes": "{" + ",".join(str(int(model.tree.node_ids[j])) for j in top) + "}",
             "probs": "{" + ",".join(f"{p[i, j]:.5f}" for j in top) + "}", "conf": float(conf[i]),
             "unc": float(ent[i]), "prio": float(priority[i])}
        )  # fmt: skip
    return rows


def score_pool(engine: Engine, model_id: int, model: HierModel) -> int:
    """Refresh suggestions + queue priority for every unlabelled pool item (idempotent upsert)."""
    with engine.begin() as conn:
        # rows of items that were labelled or became duplicates since the last scoring
        conn.execute(
            text(
                """DELETE FROM predictions p USING feedback f
                   WHERE f.id = p.feedback_id AND (f.duplicate_of IS NOT NULL
                       OR EXISTS (SELECT 1 FROM annotations a WHERE a.feedback_id = p.feedback_id))"""
            )
        )
        ids, x = unlabelled_pool(conn)
        if not ids:
            return 0
        rows = prediction_rows(model, model_id, ids, x)
        conn.execute(
            text(
                """INSERT INTO predictions (feedback_id, model_version_id, node_ids, probs, confidence, uncertainty, priority)
                   SELECT f, m, CAST(nodes AS int[]), CAST(probs AS real[]), conf, unc, prio
                   FROM unnest(CAST(:f AS bigint[]), CAST(:m AS int[]), CAST(:nodes AS text[]), CAST(:probs AS text[]),
                               CAST(:conf AS real[]), CAST(:unc AS real[]), CAST(:prio AS real[]))
                        AS t(f, m, nodes, probs, conf, unc, prio)
                   ON CONFLICT (feedback_id) DO UPDATE SET model_version_id = EXCLUDED.model_version_id,
                       node_ids = EXCLUDED.node_ids, probs = EXCLUDED.probs, confidence = EXCLUDED.confidence,
                       uncertainty = EXCLUDED.uncertainty, priority = EXCLUDED.priority"""
            ),
            {k: [r[k] for r in rows] for k in ("f", "m", "nodes", "probs", "conf", "unc", "prio")},
        )
    return len(ids)
