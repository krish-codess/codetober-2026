"""Cross-language differential fixture.

Python writes a small tiled dataset plus, for a set of awkward polygons, the selection its own
(vectorised, brute-force) implementation gets. web/src/lib/differential.test.ts loads the same
tiles and must select exactly the same points through the worker's quadtree. This test keeps the
committed fixture identical to what the code generates, so neither side can drift unnoticed.

Regenerate after a deliberate format change:  DOTS_WRITE_FIXTURE=1 pytest tests/test_web_fixture.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from dots.api import points_in_polygon, summarise_counts
from dots.tiles import build_tiles, dequantise, encode_tile, square_bounds, tile_square

OUT = Path(__file__).resolve().parent.parent / "web" / "src" / "lib" / "__fixtures__"
N, CAPACITY, SEED = 3000, 200, 7


def generate() -> dict[str, bytes]:
    # Only uniform draws and arithmetic: the bytes must come out the same on every platform.
    rng = np.random.default_rng(SEED)
    centres = rng.random((12, 2)) * 20 - 10
    xy = centres[rng.integers(0, 12, N)] + (rng.random((N, 2)) + rng.random((N, 2)) - 1) * 1.5
    xy[:300] = np.round(
        xy[:300]
    )  # points on an integer lattice, so polygon edges can pass exactly through them
    xy[300:340] = xy[300]  # forty points on one spot
    xy = xy.astype(np.float32)
    cats = np.zeros((N, 4), dtype=np.uint8)
    cats[:, 0] = rng.integers(0, 4, N)
    cats[:, 1] = rng.integers(1, 3, N)
    cats[:, 2] = rng.integers(1, 12, N)
    columns = [
        {
            "name": name,
            "labels": ["(none)"] + [f"{name}{i}" for i in range(1, size)],
            "counts": np.bincount(cats[:, ci], minlength=size).tolist(),
        }
        for ci, (name, size) in enumerate([("script", 4), ("kind", 3), ("cluster", 12)])
    ]

    bounds = square_bounds(xy)
    files: dict[str, bytes] = {}
    tiles = []
    wx, wy = np.zeros(N, np.float32), np.zeros(N, np.float32)
    for z, x, y, idx, qxy in build_tiles(xy, CAPACITY, SEED, bounds):
        files[f"{z}-{x}-{y}.bin"] = encode_tile(idx.astype(np.uint32), qxy, cats[idx])
        tiles.append([z, x, y, len(idx)])
        tx, ty, side = tile_square(bounds, z, x, y)
        wx[idx], wy[idx] = dequantise(qxy[:, 0], tx, side), dequantise(qxy[:, 1], ty, side)
    tiles.sort()

    polygons: list[list[list[float]]] = []
    for k in range(
        3, 40
    ):  # random vertex soups: mostly self-intersecting, which is what a hurried lasso looks like
        polygons.append((rng.random((k, 2)) * 24 - 12).tolist())
    for _ in range(10):  # axis-aligned boxes on the lattice: edges run exactly through lattice points
        a, b = np.sort(rng.integers(-9, 10, (2, 2)), axis=0).tolist()
        polygons.append([[a[0], a[1]], [b[0] + 1, a[1]], [b[0] + 1, b[1] + 1], [a[0], b[1] + 1]])
    for _ in range(10):  # vertices placed exactly on stored point positions
        pick = rng.integers(0, N, 5)
        polygons.append([[float(wx[i]), float(wy[i])] for i in pick])
    polygons.append([[-1e3, -1e3], [1e3, -1e3], [1e3, 1e3], [-1e3, 1e3]])  # everything
    polygons.append([[50.0, 50.0], [51.0, 50.0], [50.0, 51.0]])  # nothing
    polygons.append([[0.0, 0.0], [1e-7, 0.0], [0.0, 1e-7]])  # smaller than any gap between points

    cases = []
    for i, poly in enumerate(polygons):
        allow = None
        keep = np.ones(N, dtype=bool)
        if i % 3 == 1:  # every third case also filters by category
            allow = {"script": [1, 3], "kind": [1, 2], "cluster": list(range(1, 12, 2))}
            keep = np.isin(cats[:, 0], allow["script"]) & np.isin(cats[:, 2], allow["cluster"])
        sel = np.flatnonzero(points_in_polygon(wx, wy, [tuple(p) for p in poly]) & keep)
        summary = [c.model_dump() for c in summarise_counts(columns, cats[sel], N)]
        cases.append({"polygon": poly, "allow": allow, "ids": sel.tolist(), "summary": summary})

    doc = {
        "n": N,
        "capacity": CAPACITY,
        "bounds": list(bounds),
        "tiles": tiles,
        "columns": columns,
        "cases": cases,
    }
    files["fixture.json"] = (json.dumps(doc, separators=(",", ":")) + "\n").encode()
    return files


def test_committed_fixture_is_what_the_code_generates():
    files = generate()
    if os.environ.get("DOTS_WRITE_FIXTURE"):
        OUT.mkdir(parents=True, exist_ok=True)
        for old in OUT.iterdir():
            old.unlink()
        for name, data in files.items():
            (OUT / name).write_bytes(data)
    assert sorted(p.name for p in OUT.iterdir()) == sorted(files), (
        "run with DOTS_WRITE_FIXTURE=1 to regenerate"
    )
    for name, data in files.items():
        assert (OUT / name).read_bytes() == data, f"{name} differs from the committed fixture"


def test_fixture_cases_are_not_trivial():
    doc = json.loads(generate()["fixture.json"])
    sizes = [len(c["ids"]) for c in doc["cases"]]
    assert max(sizes) == N and min(sizes) == 0
    assert sum(1 for s in sizes if 0 < s < N) > 40
    assert len(doc["tiles"]) > 10 and max(t[0] for t in doc["tiles"]) >= 2
