"""The tiling guarantees, checked on generated point sets: nothing lost, nothing duplicated, bounded tiles."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dots.tiles import (
    MAX_DEPTH,
    Q,
    build_tiles,
    decode_tile,
    dequantise,
    encode_tile,
    load_variant,
    morton,
    square_bounds,
    tile_square,
)

coord = st.floats(-1e4, 1e4, allow_nan=False, width=32)
# Small coordinate pools force exact duplicates and points that sit exactly on split lines.
points = st.one_of(
    st.lists(st.tuples(coord, coord), min_size=1, max_size=300),
    st.lists(
        st.tuples(st.sampled_from([0.0, 1.0, 2.0, 3.0, 4.0]), st.sampled_from([0.0, 0.5, 1.0])),
        min_size=1,
        max_size=300,
    ),
)


@settings(max_examples=150, deadline=None)
@given(points, st.integers(1, 40), st.integers(0, 5))
def test_every_point_lands_in_exactly_one_tile_and_decodes_close_to_itself(pts, capacity, seed):
    xy = np.array(pts, dtype=np.float32)
    bounds = square_bounds(xy)
    seen = np.zeros(len(xy), dtype=int)
    keys = set()
    for z, x, y, idx, qxy in build_tiles(xy, capacity, seed, bounds):
        assert (z, x, y) not in keys
        keys.add((z, x, y))
        assert len(idx) <= capacity or z == MAX_DEPTH, "only the deepest level may overflow"
        seen[idx] += 1
        tx, ty, side = tile_square(bounds, z, x, y)
        # inside its tile's square...
        assert (
            (xy[idx, 0] >= tx) & (xy[idx, 0] <= tx + side) & (xy[idx, 1] >= ty) & (xy[idx, 1] <= ty + side)
        ).all()
        # ...sorted as a linear quadtree...
        codes = morton(qxy[:, 0], qxy[:, 1])
        assert (np.diff(codes.astype(np.int64)) >= 0).all()
        # ...and recoverable to within one quantisation step (plus float32 rounding of the coordinate itself).
        tol = side / Q + 1e-6 * (abs(tx) + abs(ty) + side)
        assert np.abs(dequantise(qxy[:, 0], tx, side).astype(np.float64) - xy[idx, 0]).max() <= tol
        assert np.abs(dequantise(qxy[:, 1], ty, side).astype(np.float64) - xy[idx, 1]).max() <= tol
    assert (seen == 1).all()
    for z, x, y in keys:
        assert z == 0 or (z - 1, x // 2, y // 2) in keys, (
            "a tile's parent always exists, so coarse-first loading never skips a level"
        )


@settings(max_examples=100, deadline=None)
@given(st.integers(0, 200), st.integers(0, 2**32 - 1))
def test_tile_bytes_round_trip(n, seed):
    rng = np.random.default_rng(seed)
    ids = rng.integers(0, 2**32, n, dtype=np.uint32)
    qxy = rng.integers(0, Q + 1, (n, 2), dtype=np.uint16)
    cats = rng.integers(0, 256, (n, 4), dtype=np.uint8)
    buf = encode_tile(ids, qxy, cats)
    assert len(buf) == 16 + 12 * n
    i, q, c = decode_tile(buf)
    assert (i == ids).all() and (q == qxy).all() and (c == cats).all()


@settings(max_examples=100, deadline=None)
@given(st.integers(1, 50), st.data())
def test_damaged_tiles_are_rejected_not_misread(n, data):
    buf = encode_tile(np.arange(n, dtype=np.uint32), np.zeros((n, 2), np.uint16), np.zeros((n, 4), np.uint8))
    cut = data.draw(st.integers(0, len(buf) - 1))
    with pytest.raises(ValueError):
        decode_tile(buf[:cut])  # truncated download
    with pytest.raises(ValueError):
        decode_tile(buf + b"\0")  # trailing garbage
    with pytest.raises(ValueError):
        decode_tile(b"NOPE" + buf[4:])  # some other file


def test_published_tiles_hold_every_accepted_row_once(built):
    s, _, manifest = built
    for v in manifest["variants"]:
        pts = load_variant(s.site_dir, manifest, v["name"])
        assert v["dropped_non_finite"] == 0
        assert sorted(pts["id"].tolist()) == list(range(manifest["n_points"]))
        assert sum(t[3] for t in v["tiles"]) == manifest["n_points"]
        assert max(t[3] for t in v["tiles"]) <= manifest["tile_capacity"]
        grid = np.fromfile(s.site_dir / "data" / v["tiles_path"] / "density.u32", dtype="<u4")
        assert grid.sum() == manifest["n_points"] and len(grid) == v["density_grid"] ** 2
        x0, y0, x1, y1 = v["view"]
        inside = (pts["x"] >= x0) & (pts["x"] <= x1) & (pts["y"] >= y0) & (pts["y"] <= y1)
        assert inside.mean() > 0.97, "the opening view frames nearly everything"


def test_tiling_is_reproducible_and_category_codes_are_ordered_by_frequency(built):
    s, source, manifest = built
    from dots import tiles

    again = tiles.run(s, source)
    assert again["build_id"] == manifest["build_id"], (
        "same inputs, same build id: publishing twice changes nothing"
    )
    assert again["variants"] == manifest["variants"]
    for col in manifest["columns"]:
        assert col["labels"][0] == "(none)"
        counts = col["counts"][1:]
        assert counts == sorted(counts, reverse=True)
        assert sum(col["counts"]) == manifest["n_points"]
