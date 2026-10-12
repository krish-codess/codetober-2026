"""Train every learner on the same rows, evaluate them the same way, pick a champion, score the population.

Validation is three-way so no number is reported on data that chose it:
  train  fits the models
  val    picks the champion (highest Qini among the learners that estimate uplift)
  test   is touched once, for the reported metrics, deciles, profiles and every policy evaluation

The split follows how the model is used. When units carry `assigned_at` the split is temporal (train on the earliest
60%, test on the latest 20%): the model will score people assigned after it was trained. Without timestamps
(Hillstrom has none) units are split by a hash of their id, which is stable across re-ingests.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import psycopg
from psycopg.types.json import Jsonb
from scipy.stats import chisquare
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import cross_val_score

from uplift import db, metrics, segments
from uplift.config import Settings, log, warn
from uplift.ingest import sha256_file
from uplift.learners import FEATURES, LEARNERS, featurize

logger = logging.getLogger("uplift.train")

SRM_ALPHA = 0.001  # the conventional sample-ratio-mismatch alarm level
MAX_TREATMENT_AUC = 0.55


class TrainingRefused(Exception):
    """The data cannot support an uplift model (e.g. randomization is broken). The message says why."""


def code_version() -> str:
    if v := os.environ.get("UPLIFT_CODE_VERSION"):
        return v
    try:
        root = Path(__file__).resolve().parent
        return subprocess.run(  # noqa: S603, S607 - fixed argv
            ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def load_units(conn: psycopg.Connection[Any], dataset_id: int) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT unit_id, treatment, outcome, assigned_at, recency, history::float8 AS history, mens, womens, newbie,"
        " zip_code, channel, true_uplift FROM unit WHERE dataset_id = %s ORDER BY unit_id",
        [dataset_id],
    ).fetchall()
    return pd.DataFrame(rows)


def split(df: pd.DataFrame) -> tuple[pd.Series, str]:
    if df["assigned_at"].notna().all():
        rank = df.sort_values(["assigned_at", "unit_id"]).index
        position = pd.Series(np.arange(len(df)) / len(df), index=rank).reindex(df.index)
        return pd.cut(position, [-1, 0.6, 0.8, 2], labels=["train", "val", "test"], right=False).astype(str), "temporal"
    bucket = df["unit_id"].map(lambda u: int(hashlib.sha256(u.encode()).hexdigest()[:8], 16) % 100)
    return pd.cut(bucket, [-1, 59, 79, 100], labels=["train", "val", "test"]).astype(str), "hash"


def audit(df: pd.DataFrame, X: np.ndarray, treated_share: float, seed: int) -> dict[str, Any]:
    """Is this still a randomized experiment, and did anything from after assignment leak into the features?

    If features can predict who was treated, either randomization failed or a feature was recorded after treatment
    (a leak). Both show up as a treatment AUC away from 0.5, so one check covers both.
    """
    t = df["treatment"].to_numpy(float)
    n, n_treated = len(t), int(t.sum())
    srm_p = float(chisquare([n_treated, n - n_treated], [n * treated_share, n * (1 - treated_share)]).pvalue)
    sd = X.std(axis=0)
    smd = np.abs(X[t == 1].mean(axis=0) - X[t == 0].mean(axis=0)) / np.where(sd > 0, sd, 1)
    sample = np.random.default_rng(seed).choice(n, min(n, 40_000), replace=False)
    auc = float(
        cross_val_score(
            HistGradientBoostingClassifier(max_iter=50, max_depth=3, random_state=seed),
            X[sample],
            t[sample],
            cv=3,
            scoring="roc_auc",
        ).mean()
    )
    problems = []
    if srm_p < SRM_ALPHA:
        problems.append(
            f"sample ratio mismatch: {n_treated / n:.4f} of units are treated, the design says {treated_share:.4f} (p={srm_p:.2g})"
        )
    if auc > MAX_TREATMENT_AUC:
        problems.append(f"features predict treatment (AUC {auc:.3f}): randomization is broken or a feature leaks")
    return {
        "units": n,
        "treated_share": n_treated / n,
        "expected_treated_share": treated_share,
        "srm_p_value": srm_p,
        "max_standardized_mean_difference": float(smd.max()),
        "standardized_mean_differences": dict(zip(FEATURES, (round(float(v), 4) for v in smd), strict=True)),
        "treatment_auc": auc,
        "missing_share": {
            c: round(float(df[c].isna().mean()), 4) for c in ("recency", "history", "zip_code", "channel")
        },
        "outcome_mean": float(df["outcome"].mean()),
        "problems": problems,
    }


def _evaluate(
    df: pd.DataFrame, scores: dict[str, np.ndarray], uplift_learners: set[str], rounds: int, seed: int
) -> dict[str, Any]:
    """Metrics for every learner on one split, with paired-bootstrap uncertainty."""
    y, t = df["outcome"].to_numpy(float), df["treatment"].to_numpy(float)
    true = df["true_uplift"].to_numpy(float) if df["true_uplift"].notna().all() else None
    boot = metrics.bootstrap_qini(y, t, scores, rounds, seed)
    out: dict[str, Any] = {}
    for name, s in scores.items():
        m: dict[str, Any] = {
            "qini": metrics.qini(y, t, s),
            "qini_ci95": [float(np.percentile(boot[name], 2.5)), float(np.percentile(boot[name], 97.5))],
            "auuc": metrics.auuc(y, t, s),
            "uplift_at": {str(f): metrics.uplift_at(y, t, s, f) for f in (0.1, 0.2, 0.3)},
        }
        for baseline in ("random", "response"):
            if baseline in boot and name != baseline:
                m[f"p_beats_{baseline}"] = float((boot[name] > boot[baseline]).mean())
        if true is not None:
            m["qini_share_of_oracle"] = m["qini"] / metrics.qini(y, t, true)
            if name in uplift_learners:
                m["pehe"] = float(np.sqrt(np.mean((s - true) ** 2)))
        out[name] = m
    return out


def train(
    settings: Settings,
    dataset: str,
    learners: list[str] | None = None,
    seed: int = 7,
    rounds: int = 200,
    treated_share: float = 2 / 3,
) -> dict[str, Any]:
    names = learners or list(LEARNERS)
    unknown = [n for n in names if n not in LEARNERS]
    if unknown:
        raise SystemExit(f"unknown learners: {unknown}; choose from {list(LEARNERS)}")
    timings: dict[str, float] = {}
    clock = time.perf_counter()

    def lap(stage: str) -> None:
        nonlocal clock
        timings[stage] = round(time.perf_counter() - clock, 2)
        clock = time.perf_counter()

    exp_id = uuid.uuid4()
    conn = db.connect(settings.batch_database_url)
    with conn:
        ds = conn.execute("SELECT id FROM dataset WHERE name = %s", [dataset]).fetchone()
        if ds is None:
            raise SystemExit(f"no dataset named {dataset!r}; ingest it first")
        version = db.data_version(conn, ds["id"])
        params = {
            "learners": names,
            "seed": seed,
            "bootstrap_rounds": rounds,
            "treated_share": treated_share,
            "features": FEATURES,
        }
        conn.execute(
            "INSERT INTO experiment (id, dataset_id, status, data_version, code_version, params) VALUES (%s, %s, 'running', %s, %s, %s)",
            [exp_id, ds["id"], version, code_version(), Jsonb(params)],
        )
        conn.commit()  # visible while it runs; a crash leaves a 'running' row that the next line of defence fails
        try:
            result = _run(settings, conn, exp_id, ds["id"], names, seed, rounds, treated_share, timings, lap)
        except BaseException as exc:
            conn.rollback()
            conn.execute(
                "UPDATE experiment SET status = 'failed', finished_at = now(), audit = coalesce(audit, '{}'::jsonb) || %s WHERE id = %s",
                [Jsonb({"error": str(exc)[:2000]}), exp_id],
            )
            conn.commit()
            if isinstance(exc, TrainingRefused):
                raise SystemExit(f"training refused: {exc}") from exc
            raise
    return result


def _run(
    settings: Settings,
    conn: psycopg.Connection[Any],
    exp_id: uuid.UUID,
    dataset_id: int,
    names: list[str],
    seed: int,
    rounds: int,
    treated_share: float,
    timings: dict[str, float],
    lap: Any,
) -> dict[str, Any]:
    df = load_units(conn, dataset_id)
    if len(df) < 5000:
        raise TrainingRefused(f"only {len(df)} valid units; uplift needs thousands per arm to see a few-point effect")
    parts, strategy = split(df)
    is_train = (parts == "train").to_numpy()
    _, medians = featurize(df[is_train])
    X, _ = featurize(df, medians)
    y, t = df["outcome"].to_numpy(float), df["treatment"].to_numpy(float)
    lap("load_and_featurize")

    checks = audit(df, X, treated_share, seed)
    conn.execute(
        "UPDATE experiment SET audit = %s, split_strategy = %s WHERE id = %s", [Jsonb(checks), strategy, exp_id]
    )
    conn.commit()
    if checks["problems"]:
        raise TrainingRefused("; ".join(checks["problems"]))
    lap("audit")

    artifact_dir = settings.artifact_dir / str(exp_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    scores: dict[str, np.ndarray] = {}
    runs: dict[str, dict[str, Any]] = {}
    for name in names:
        started = time.perf_counter()
        try:
            model = LEARNERS[name](seed=seed).fit(X[is_train], t[is_train], y[is_train])
            scores[name] = model.predict(X)
            path = artifact_dir / f"{name}.joblib"
            joblib.dump({"learner": name, "model": model, "medians": medians, "features": FEATURES}, path)
            runs[name] = {
                "status": "complete",
                "artifact_file": f"{exp_id}/{name}.joblib",
                "artifact_sha256": sha256_file(path),
            }
        except Exception as exc:  # noqa: BLE001 - one learner failing must not cost the others their results
            warn(logger, "learner failed", learner=name, error=str(exc))
            runs[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:1000]}
        runs[name]["fit_seconds"] = round(time.perf_counter() - started, 2)
        timings[f"fit_{name}"] = runs[name]["fit_seconds"]
        log(logger, "learner finished", learner=name, status=runs[name]["status"], seconds=runs[name]["fit_seconds"])
    lap("fit_total")

    uplift_learners = {n for n in scores if LEARNERS[n].estimates_uplift}
    if not uplift_learners:
        raise TrainingRefused("no uplift learner trained successfully")
    evaluated = {}
    for part in ("val", "test"):
        mask = (parts == part).to_numpy()
        evaluated[part] = _evaluate(df[mask], {n: s[mask] for n, s in scores.items()}, uplift_learners, rounds, seed)
    champion = max(uplift_learners, key=lambda n: evaluated["val"][n]["qini"])

    test = (parts == "test").to_numpy()
    test_df = df[test]
    yt, tt = y[test], t[test]
    true = test_df["true_uplift"].to_numpy(float) if test_df["true_uplift"].notna().all() else None
    for name, s in scores.items():
        dec = metrics.deciles(yt, tt, s[test], true)
        predicted, observed = [d["mean_predicted"] for d in dec], [d["observed_uplift"] for d in dec]
        runs[name].update(
            metrics={
                "estimates_uplift": name in uplift_learners,
                "val": evaluated["val"][name],
                "test": evaluated["test"][name],
                # slope of observed on predicted uplift across deciles: 1 is calibrated, <1 is over-confident
                "calibration_slope": float(np.polyfit(predicted, observed, 1)[0]) if name in uplift_learners else None,  # type: ignore[arg-type]
            },
            curves={
                "qini": metrics.thin(*metrics.qini_curve(yt, tt, s[test])),
                "uplift": metrics.thin(*metrics.uplift_curve(yt, tt, s[test])),
            },
            deciles=dec,
            profiles=segments.profiles(test_df, s[test], name in uplift_learners),
        )
    lap("evaluate")
    found = segments.scan(df)
    lap("segment_scan")

    reference = {
        "ate_test": metrics.diff_in_means(yt, tt),
        "split_sizes": {k: int(v) for k, v in parts.value_counts().items()},
        "oracle_qini_test": None if true is None else metrics.qini(yt, tt, true),
        "champion_beats_response_on_val": evaluated["val"][champion]["qini"]
        > evaluated["val"].get("response", {}).get("qini", -np.inf),
    }
    with conn.transaction():
        for name, r in runs.items():
            run_id = uuid.uuid4()
            conn.execute(
                "INSERT INTO model_run (id, experiment_id, learner, status, error, is_champion, artifact_file, artifact_sha256,"
                " fit_seconds, metrics, curves, profiles) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    run_id, exp_id, name, r["status"], r.get("error"), name == champion, r.get("artifact_file"),
                    r.get("artifact_sha256"), r["fit_seconds"],
                    Jsonb(r["metrics"]) if "metrics" in r else None,
                    Jsonb(r["curves"]) if "curves" in r else None,
                    Jsonb(r["profiles"]) if "profiles" in r else None,
                ],
            )  # fmt: skip
            for d in r.get("deciles", []):
                conn.execute(
                    "INSERT INTO decile (run_id, decile, n_treated, n_control, mean_predicted, observed_uplift, ci_low, ci_high, true_uplift)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        run_id,
                        *[
                            d[k]
                            for k in (
                                "decile",
                                "n_treated",
                                "n_control",
                                "mean_predicted",
                                "observed_uplift",
                                "ci_low",
                                "ci_high",
                                "true_uplift",
                            )
                        ],
                    ],
                )
        for seg in found:
            conn.execute(
                "INSERT INTO segment (experiment_id, definition, n_treated, n_control, uplift, ci_low, ci_high, p_adjusted, verdict)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    exp_id,
                    *[
                        seg[k]
                        for k in (
                            "definition",
                            "n_treated",
                            "n_control",
                            "uplift",
                            "ci_low",
                            "ci_high",
                            "p_adjusted",
                            "verdict",
                        )
                    ],
                ],
            )
        lap("persist")
        conn.execute(
            "UPDATE experiment SET status = 'complete', finished_at = now(), timings = %s, audit = audit || %s WHERE id = %s",
            [Jsonb(timings), Jsonb({"reference": reference}), exp_id],
        )
    summary = {
        "experiment": str(exp_id),
        "units": len(df),
        "split": strategy,
        "champion": champion,
        "qini_test": {n: round(evaluated["test"][n]["qini"], 5) for n in scores},
        "failed": [n for n, r in runs.items() if r["status"] == "failed"],
        "segments_that_hurt": [s["definition"] for s in found if s["verdict"] == "hurts"],
        "timings": timings,
    }
    log(logger, "experiment complete", **summary)
    return summary


def load_artifact(settings: Settings, artifact_file: str, expected_sha256: str) -> dict[str, Any]:
    """Load a model, but only the exact bytes the training run recorded: an artifact is a pickle, and unpickling
    a file someone swapped in is code execution."""
    path = settings.artifact_dir / artifact_file
    if not path.is_file():
        raise FileNotFoundError(f"model artifact {artifact_file} is not on this machine")
    if sha256_file(path) != expected_sha256:
        raise ValueError(f"model artifact {artifact_file} does not match its recorded SHA-256; refusing to load it")
    return joblib.load(path)


def score(settings: Settings, dataset: str, chunk: int = 50_000) -> dict[str, Any]:
    """Batch scoring: the champion of the latest complete experiment scores every unit. Replaces its own previous
    output in one transaction, so it can be re-run at any time."""
    started = time.perf_counter()
    with db.connect(settings.batch_database_url) as conn:
        run = conn.execute(
            "SELECT r.id, r.learner, r.artifact_file, r.artifact_sha256, e.dataset_id, e.data_version FROM model_run r"
            " JOIN experiment e ON e.id = r.experiment_id JOIN dataset d ON d.id = e.dataset_id"
            " WHERE d.name = %s AND e.status = 'complete' AND r.is_champion ORDER BY e.finished_at DESC LIMIT 1",
            [dataset],
        ).fetchone()
        if run is None:
            raise SystemExit(f"no complete experiment for dataset {dataset!r}; train first")
        if db.data_version(conn, run["dataset_id"]) != run["data_version"]:
            raise SystemExit("the dataset has new files since this model was trained; retrain before scoring")
        artifact = load_artifact(settings, run["artifact_file"], run["artifact_sha256"])
        df = load_units(conn, run["dataset_id"])
        parts, _ = split(df)
        X, _ = featurize(df, artifact["medians"])
        uplift = np.concatenate([artifact["model"].predict(X[i : i + chunk]) for i in range(0, len(X), chunk)])
        conn.execute("DELETE FROM score WHERE run_id = %s", [run["id"]])
        with conn.cursor().copy("COPY score (run_id, dataset_id, unit_id, split, uplift) FROM STDIN") as copy:
            for unit_id, part, u in zip(df["unit_id"], parts, uplift, strict=True):
                copy.write_row([run["id"], run["dataset_id"], unit_id, part, float(u)])
    seconds = time.perf_counter() - started
    result = {
        "run": str(run["id"]),
        "learner": run["learner"],
        "rows": len(df),
        "seconds": round(seconds, 2),
        "rows_per_second": round(len(df) / seconds),
        "share_predicted_negative": round(float((uplift < 0).mean()), 4),
    }
    log(logger, "scored", dataset=dataset, **result)
    return result
