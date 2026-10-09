"""Projection and evaluation: smoke, contract, harness correctness, and the regression gate."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dots import project
from dots.config import read_json
from dots.evaluate import exact_knn, knn_2d, purity, recall
from dots.project import landmark_ids, load_vectors, parse_variant, pca

BASELINE = json.loads((Path(__file__).parent / "eval_baseline.json").read_text(encoding="utf-8"))


def test_projection_smoke_and_output_contract(built):
    """Training smoke test + inference contract: every variant is (n, 2) float32, finite, one row per id."""
    s, source, manifest = built
    n = manifest["n_points"]
    for name in s.variants:
        xy = np.load(s.work(source) / "proj" / f"{name}.npy")
        assert xy.shape == (n, 2) and xy.dtype == np.float32
        assert np.isfinite(xy).all()
        assert xy.std(axis=0).min() > 0, "a projection that collapses to a line is not a projection"
        info = read_json(s.work(source) / "proj" / f"{name}.json")
        assert info["rows"] == n and info["seconds"] >= 0
    info = read_json(s.work(source) / "proj" / "umap-n10-d0.1.json")
    assert info["placed_rows_per_s"] > 0, "rows outside the landmark sample were placed by transform"


def test_projection_stage_is_idempotent(built):
    s, source, _ = built
    stamps = {p.name: p.stat().st_mtime_ns for p in (s.work(source) / "proj").glob("*.npy")}
    project.run(s, source)
    assert {p.name: p.stat().st_mtime_ns for p in (s.work(source) / "proj").glob("*.npy")} == stamps


def test_pca_is_deterministic_and_orders_variance(built):
    s, source, _ = built
    x = load_vectors(s, source)
    a, info = pca(x)
    b, _ = pca(x)
    assert np.array_equal(a, b)
    assert a[:, 0].var() >= a[:, 1].var()
    assert 0 < info["explained_variance"] < 1


def test_landmarks_are_seeded_and_variants_parse():
    assert np.array_equal(landmark_ids(1000, 100, 7), landmark_ids(1000, 100, 7))
    assert len(set(landmark_ids(1000, 100, 7).tolist())) == 100
    assert len(landmark_ids(50, 100, 7)) == 50
    assert parse_variant("umap-n50-d0.5") == {"method": "umap", "n_neighbors": 50, "min_dist": 0.5}
    with pytest.raises(SystemExit):
        parse_variant("tsne")


def test_neighbour_search_matches_brute_force():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(400, 8)).astype(np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    q = np.arange(0, 400, 7)
    got = exact_knn(x, q, 5, chunk=16)
    d = ((x[q][:, None, :] - x[None, :, :]) ** 2).sum(-1)
    d[np.arange(len(q)), q] = np.inf
    assert [set(r) for r in got.tolist()] == [set(r) for r in np.argsort(d, axis=1)[:, :5].tolist()]

    xy = rng.normal(size=(400, 2)).astype(np.float32)
    xy[3] = np.nan  # a row the projection failed to place
    low = knn_2d(xy, q, 5)
    d2 = ((xy[q][:, None, :] - xy[None, :, :]) ** 2).sum(-1)
    d2[np.arange(len(q)), q] = np.inf
    d2[:, 3] = np.inf
    assert [set(r) for r in low.tolist()] == [set(r) for r in np.argsort(d2, axis=1)[:, :5].tolist()]
    assert (knn_2d(xy, np.array([3]), 5) == -1).all(), "an unplaced row has no neighbours and scores zero"


def test_metrics_at_their_extremes():
    nn = np.array([[1, 2, 3], [0, 2, 3]])
    assert recall(nn, nn).tolist() == [1.0, 1.0]
    assert recall(nn, np.array([[7, 8, 9], [0, 8, 9]])).tolist() == [0.0, 1 / 3]
    labels = np.array([1, 1, 1, 2, 2])
    assert purity(labels, np.array([0, 3]), np.array([[1, 2], [0, 4]])).tolist() == [1.0, 0.5]
    assert purity(labels, np.array([0]), np.array([[-1, -1]])).tolist() == [0.0]


def test_evaluation_reports_baselines_intervals_and_the_fitted_placed_split(built):
    s, source, _ = built
    ev = read_json(s.work(source) / "eval.json")
    assert ev["queries"] == s.eval_queries and ev["chance"]["recall"] < 0.01
    umap, base = ev["variants"]["umap-n10-d0.1"], ev["variants"]["pca"]
    for v in (umap, base):
        r = v["recall"]["all"]
        assert 0 <= r["lo"] <= r["mean"] <= r["hi"] <= 1
    assert base["recall"]["fitted"] is None and base["recall_vs_pca"] is None
    assert umap["recall"]["fitted"]["n"] + umap["recall"]["placed"]["n"] == ev["queries"]
    assert set(umap["purity"]) == {"script", "kind", "cluster"}
    lines = (s.work(source) / "experiments.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["variants"].keys() == ev["variants"].keys(), (
        "every run is appended to the experiment log"
    )


def test_evaluation_regression_gate(built):
    """Fails when a metric that matters drops by more than the tolerance below the committed baseline."""
    s, source, _ = built
    ev = read_json(s.work(source) / "eval.json")
    tol = BASELINE["tolerance"]
    for variant, metrics in BASELINE["variants"].items():
        for metric, floor in metrics.items():
            node = ev["variants"][variant]
            for part in metric.split("."):
                node = node[part]
            assert node["mean"] >= floor - tol, (
                f"{variant} {metric} fell to {node['mean']} (baseline {floor}, tolerance {tol})"
            )
    umap, base = ev["variants"]["umap-n10-d0.1"], ev["variants"]["pca"]
    # The model has to earn its place: clearly better than the linear baseline, which is clearly better than chance.
    assert umap["recall_vs_pca"]["lo"] > 0
    assert umap["purity"]["cluster"]["lo"] > base["purity"]["cluster"]["hi"]
    assert base["purity"]["cluster"]["lo"] > ev["chance"]["purity"]["cluster"]
