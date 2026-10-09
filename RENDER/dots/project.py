"""Stage 3: project the vectors to 2-D, once per parameter variant, plus k-means labels.

Variants (DOTS_VARIANTS):
  pca              linear baseline: top two principal components of the unit-normalised vectors
  umap-n<N>-d<D>   UMAP with n_neighbors=N, min_dist=D

UMAP is fitted on a seeded sample of `DOTS_LANDMARKS` rows and every other row is placed with
`transform`, in chunks. Fitting a million rows directly needs more memory than the machine this
was built on has free, and placing unseen rows is how a projection is used once new data arrives,
so the evaluation reports fitted and placed rows separately.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import numpy as np

from .config import Settings, event, read_json, write_json

N_CLUSTERS = 24
CHUNK = 50_000


def load_vectors(s: Settings, source: str) -> np.ndarray:
    """Unit-normalised vectors, so Euclidean distance orders neighbours the same way cosine does."""
    info = read_json(s.work(source) / "ingest.json")
    raw = np.memmap(
        s.work(source) / "vectors.f32", dtype=np.float32, mode="r", shape=(info["accepted"], info["dim"])
    )
    x = np.array(raw)
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    return x


def landmark_ids(n: int, k: int, seed: int) -> np.ndarray:
    return np.sort(np.random.default_rng(seed).choice(n, min(k, n), replace=False))


def parse_variant(name: str) -> dict:
    if name == "pca":
        return {"method": "pca"}
    m = re.fullmatch(r"umap-n(\d+)-d(\d*\.?\d+)", name)
    if not m:
        raise SystemExit(f"unknown variant {name!r}: expected pca or umap-n<neighbours>-d<min_dist>")
    return {"method": "umap", "n_neighbors": int(m[1]), "min_dist": float(m[2])}


def pca(x: np.ndarray) -> tuple[np.ndarray, dict]:
    mean = x.mean(axis=0)
    cov = np.cov(x - mean, rowvar=False)
    values, vectors = np.linalg.eigh(cov)
    top = vectors[:, ::-1][:, :2]
    return ((x - mean) @ top).astype(np.float32), {
        "explained_variance": float(values[::-1][:2].sum() / values.sum())
    }


def umap_landmark(x: np.ndarray, params: dict, landmarks: np.ndarray, seed: int) -> tuple[np.ndarray, dict]:
    import umap

    t0 = time.perf_counter()
    # A fixed random_state makes UMAP single-threaded; that is the price of a reproducible build.
    model = umap.UMAP(
        n_neighbors=params["n_neighbors"], min_dist=params["min_dist"], metric="euclidean", random_state=seed
    )
    out = np.empty((len(x), 2), dtype=np.float32)
    out[landmarks] = model.fit_transform(x[landmarks])
    fit_s = time.perf_counter() - t0
    rest = np.setdiff1d(np.arange(len(x)), landmarks, assume_unique=True)
    t1 = time.perf_counter()
    for i in range(0, len(rest), CHUNK):
        ids = rest[i : i + CHUNK]
        out[ids] = model.transform(x[ids])
        event(
            "umap transform",
            done=min(i + CHUNK, len(rest)),
            of=len(rest),
            elapsed_s=round(time.perf_counter() - t1),
        )
    place_s = time.perf_counter() - t1
    return out, {
        "fit_seconds": round(fit_s, 1),
        "place_seconds": round(place_s, 1),
        "placed_rows_per_s": round(len(rest) / place_s) if len(rest) else None,
    }


def run(s: Settings, source: str, force: bool = False) -> None:
    work = s.work(source)
    ingest = read_json(work / "ingest.json")
    n = ingest["accepted"]
    proj = work / "proj"
    proj.mkdir(exist_ok=True)
    landmarks = landmark_ids(n, s.landmarks, s.seed)
    x: np.ndarray | None = None

    def vectors() -> np.ndarray:
        nonlocal x
        if x is None:
            x = load_vectors(s, source)
        return x

    cluster_path = work / "cluster.npy"
    key = {"raw": ingest["raw"], "seed": s.seed, "k": N_CLUSTERS}
    if force or not cluster_path.exists() or read_json(work / "cluster.json").get("key") != key:
        from sklearn.cluster import MiniBatchKMeans

        t0 = time.perf_counter()
        km = MiniBatchKMeans(n_clusters=min(N_CLUSTERS, n), random_state=s.seed, n_init=3, batch_size=8192)
        labels = km.fit_predict(vectors())
        np.save(cluster_path, (labels + 1).astype(np.uint8))  # code 0 is reserved for "no value"
        write_json(
            work / "cluster.json",
            {"key": key, "seconds": round(time.perf_counter() - t0, 1), "inertia": float(km.inertia_)},
        )
        event("clustered", k=N_CLUSTERS, seconds=round(time.perf_counter() - t0, 1))

    for name in s.variants:
        params = parse_variant(name)
        key = {
            "raw": ingest["raw"],
            "seed": s.seed,
            "params": params,
            "landmarks": len(landmarks) if params["method"] == "umap" else None,
        }
        info_path: Path = proj / f"{name}.json"
        if (
            not force
            and info_path.exists()
            and (proj / f"{name}.npy").exists()
            and read_json(info_path)["key"] == key
        ):
            event("projection skipped, already current", variant=name)
            continue
        t0 = time.perf_counter()
        xy, extra = (
            pca(vectors())
            if params["method"] == "pca"
            else umap_landmark(vectors(), params, landmarks, s.seed)
        )
        secs = time.perf_counter() - t0
        np.save(proj / f"{name}.npy", xy)
        info = {
            "key": key,
            "variant": name,
            "rows": n,
            "seconds": round(secs, 1),
            "rows_per_s": round(n / secs),
            "non_finite": int((~np.isfinite(xy).all(axis=1)).sum()),
            **extra,
        }
        write_json(info_path, info)
        event("projected", **{k: v for k, v in info.items() if k != "key"})
