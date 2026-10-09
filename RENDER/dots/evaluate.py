"""Stage 4: how much neighbourhood structure does each projection keep, against a baseline?

For a seeded sample of query rows, with exact neighbours on both sides:

* recall@k  share of a row's k nearest neighbours in 50-d that are also among its k nearest in 2-D
* purity@k  share of a row's k nearest 2-D neighbours carrying the row's own label
            (script, kind, k-means cluster)

Reference points: chance (random neighbours), PCA (the linear baseline every variant is compared
with, paired on the same queries), and purity measured in the original 50-d space (what a
projection could at best preserve). Intervals are 95% bootstrap over queries. UMAP queries are
split into rows the model was fitted on and rows it only placed afterwards.
"""

from __future__ import annotations

import json
import subprocess
import time

import numpy as np
import pyarrow.parquet as pq

from .config import REPO, Settings, event, read_json, write_json
from .project import landmark_ids, load_vectors, parse_variant
from .tiles import COLUMNS, encode_categories

K = 15
BOOTSTRAP = 2000


def exact_knn(x: np.ndarray, queries: np.ndarray, k: int, chunk: int = 32) -> np.ndarray:
    """Brute-force k nearest neighbours of unit vectors by dot product. (len(queries), k) row ids."""
    out = np.empty((len(queries), k), dtype=np.int64)
    for i in range(0, len(queries), chunk):
        q = queries[i : i + chunk]
        sims = x[q] @ x.T
        sims[np.arange(len(q)), q] = -np.inf  # a row is not its own neighbour
        out[i : i + chunk] = np.argpartition(-sims, k, axis=1)[:, :k]
    return out


def knn_2d(xy: np.ndarray, queries: np.ndarray, k: int) -> np.ndarray:
    """Exact 2-D neighbours with a k-d tree. Rows without a finite position have none (-1)."""
    from scipy.spatial import cKDTree

    ok = np.flatnonzero(np.isfinite(xy).all(axis=1))
    out = np.full((len(queries), k), -1, dtype=np.int64)
    if len(ok) <= k:
        return out
    tree = cKDTree(xy[ok])
    placed = np.isfinite(xy[queries]).all(axis=1)
    _, nn = tree.query(xy[queries[placed]], k + 1)
    nn = ok[nn]
    for row, q, cand in zip(np.flatnonzero(placed), queries[placed], nn, strict=True):
        out[row] = cand[cand != q][:k]  # drop the query itself, wherever exact ties put it
    return out


def ci(values: np.ndarray, rng: np.random.Generator) -> dict | None:
    if len(values) == 0:
        return None
    means = values[rng.integers(0, len(values), (BOOTSTRAP, len(values)))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {
        "mean": round(float(values.mean()), 4),
        "lo": round(float(lo), 4),
        "hi": round(float(hi), 4),
        "n": int(len(values)),
    }


def recall(high: np.ndarray, low: np.ndarray) -> np.ndarray:
    return (
        np.array([len(np.intersect1d(h, low_row)) for h, low_row in zip(high, low, strict=True)])
        / high.shape[1]
    )


def purity(labels: np.ndarray, queries: np.ndarray, nn: np.ndarray) -> np.ndarray:
    same = (labels[np.maximum(nn, 0)] == labels[queries][:, None]) & (nn >= 0)
    return same.mean(axis=1)


def git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()  # noqa: S603, S607
    except (OSError, subprocess.CalledProcessError):
        return None


def run(s: Settings, source: str) -> dict:
    work = s.work(source)
    t0 = time.perf_counter()
    x = load_vectors(s, source)
    n = len(x)
    k = min(K, n - 2)
    rng = np.random.default_rng(s.seed + 1)
    landmarks = landmark_ids(n, s.landmarks, s.seed)
    others = np.setdiff1d(np.arange(n), landmarks, assume_unique=True)
    half = s.eval_queries // 2
    queries = np.concatenate(
        [
            rng.choice(landmarks, min(half, len(landmarks)), replace=False),
            rng.choice(others, min(half, len(others)), replace=False),
        ]
    )
    fitted = np.isin(queries, landmarks)

    cats, columns = encode_categories(pq.read_table(work / "meta.parquet"), np.load(work / "cluster.npy"))
    high = exact_knn(x, queries, k)
    chance_purity, high_purity = {}, {}
    for ci_, name in enumerate(COLUMNS):
        p = np.bincount(cats[:, ci_]) / n
        chance_purity[name] = round(float((p**2).sum()), 4)
        high_purity[name] = ci(purity(cats[:, ci_], queries, high), rng)

    per_query: dict[str, np.ndarray] = {}
    variants: dict[str, dict] = {}
    for name in s.variants:
        low = knn_2d(np.load(work / "proj" / f"{name}.npy"), queries, k)
        r = recall(high, low)
        per_query[name] = r
        is_umap = parse_variant(name)["method"] == "umap"
        variants[name] = {
            "recall": {
                "all": ci(r, rng),
                "fitted": ci(r[fitted], rng) if is_umap else None,
                "placed": ci(r[~fitted], rng) if is_umap else None,
            },
            "purity": {col: ci(purity(cats[:, i], queries, low), rng) for i, col in enumerate(COLUMNS)},
        }
    for name, v in variants.items():
        # Paired on the same queries: the interval is on the per-query difference, not on two means.
        v["recall_vs_pca"] = (
            ci(per_query[name] - per_query["pca"], rng) if "pca" in per_query and name != "pca" else None
        )

    result = {
        "k": k,
        "queries": int(len(queries)),
        "bootstrap": BOOTSTRAP,
        "chance": {"recall": round(k / (n - 1), 6), "purity": chance_purity},
        "high_dim_purity": high_purity,
        "seconds": round(time.perf_counter() - t0, 1),
        "variants": variants,
    }
    write_json(work / "eval.json", result)
    # One line per run, never rewritten: data, code and parameters next to the numbers they produced.
    record = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "code": git_sha(),
        "seed": s.seed,
        "landmarks": int(len(landmarks)),
        "raw": read_json(work / "ingest.json")["raw"],
        **result,
    }
    with (work / "experiments.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    event(
        "evaluated",
        seconds=result["seconds"],
        **{name: v["recall"]["all"]["mean"] for name, v in variants.items()},
    )
    return result
