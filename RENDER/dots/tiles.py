"""Stage 5: cut each projection into level-of-detail quadtree tiles served as binary buffers.

The tree is additive: every point lives in exactly one tile. The root holds a random sample of
`capacity` points from the whole plane, each child the next `capacity` points that fall in its
quadrant, and so on. Drawing depths 0..d shows a sample that gets denser with d, and a client
that stops at a point budget holds a bounded amount no matter how large the dataset is.

Tile file, little-endian, 16 + 12n bytes:

    0   u32  magic "DOTS"
    4   u16  format version (1)
    6   u16  bytes of category codes per point (4)
    8   u32  n, the number of points
    12  u32  reserved, 0
    16  u32[n]    row id (the key into meta.parquet)
    ..  u16[2n]   x, y quantised to the tile's own square, interleaved
    ..  u8[4n]    category codes: script, kind, cluster, 0

Points are sorted by the Morton code of their quantised position, so the array is a linear
quadtree: every quadtree cell of the tile is one contiguous run, found by binary search.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .config import Settings, event, read_json, write_json
from .ingest import ROW_GROUP
from .project import parse_variant

MAGIC = b"DOTS"
FORMAT = 1
CAT_BYTES = 4
HEADER = struct.Struct("<4sHHII")
MAX_DEPTH = 14
GRID = 256
COLUMNS = ("script", "kind", "cluster")
Q = 65535


def morton(qx: np.ndarray, qy: np.ndarray) -> np.ndarray:
    """Interleave two u16 arrays into u32 codes: x in the even bits, y in the odd bits."""

    def spread(v: np.ndarray) -> np.ndarray:
        v = v.astype(np.uint32)
        v = (v | (v << 8)) & 0x00FF00FF
        v = (v | (v << 4)) & 0x0F0F0F0F
        v = (v | (v << 2)) & 0x33333333
        return (v | (v << 1)) & 0x55555555

    return spread(qx) | (spread(qy) << 1)


def tile_square(bounds: tuple[float, float, float], z: int, x: int, y: int) -> tuple[float, float, float]:
    """(x0, y0, size) of a tile in world units. Powers of two keep parent and child edges identical."""
    x0, y0, size = bounds
    side = size / (1 << z)
    return x0 + x * side, y0 + y * side, side


def quantise(v: np.ndarray, origin: float, side: float) -> np.ndarray:
    return np.clip(np.rint((v.astype(np.float64) - origin) / side * Q), 0, Q).astype(np.uint16)


def dequantise(q: np.ndarray, origin: float, side: float) -> np.ndarray:
    """Float32 world coordinate. web/src/lib/tile.ts does the same arithmetic in the same order."""
    return (origin + q.astype(np.float64) * (side / Q)).astype(np.float32)


def encode_tile(ids: np.ndarray, qxy: np.ndarray, cats: np.ndarray) -> bytes:
    n = len(ids)
    assert qxy.shape == (n, 2) and cats.shape == (n, CAT_BYTES)
    return (
        HEADER.pack(MAGIC, FORMAT, CAT_BYTES, n, 0)
        + ids.astype("<u4").tobytes()
        + qxy.astype("<u2").tobytes()
        + cats.astype(np.uint8).tobytes()
    )


def decode_tile(buf: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if len(buf) < HEADER.size:
        raise ValueError("tile shorter than its header")
    magic, version, cat_bytes, n, _ = HEADER.unpack_from(buf)
    if magic != MAGIC or version != FORMAT or cat_bytes != CAT_BYTES:
        raise ValueError(f"not a format-{FORMAT} tile")
    if len(buf) != HEADER.size + n * (8 + CAT_BYTES):
        raise ValueError(f"tile says {n} points but is {len(buf)} bytes")
    ids = np.frombuffer(buf, "<u4", n, HEADER.size)
    qxy = np.frombuffer(buf, "<u2", 2 * n, HEADER.size + 4 * n).reshape(n, 2)
    cats = np.frombuffer(buf, np.uint8, CAT_BYTES * n, HEADER.size + 8 * n).reshape(n, CAT_BYTES)
    return ids, qxy, cats


def square_bounds(xy: np.ndarray) -> tuple[float, float, float]:
    lo, hi = xy.min(axis=0).astype(np.float64), xy.max(axis=0).astype(np.float64)
    size = float(max((hi - lo).max(), 1e-9)) * (1 + 1e-6)  # the maximum must land strictly inside
    cx, cy = (lo + hi) / 2
    return float(cx - size / 2), float(cy - size / 2), size


def build_tiles(xy: np.ndarray, capacity: int, seed: int, bounds: tuple[float, float, float] | None = None):
    """Yield (z, x, y, row_indices_in_morton_order, qxy) for every tile. `xy` must be finite."""
    if len(xy) == 0:
        return
    bounds = bounds or square_bounds(xy)
    order = np.random.default_rng(seed).permutation(len(xy))  # a point's priority decides its depth
    stack = [(0, 0, 0, order)]
    while stack:
        z, x, y, idx = stack.pop()
        take, rest = (idx, idx[:0]) if z == MAX_DEPTH else (idx[:capacity], idx[capacity:])
        tx, ty, side = tile_square(bounds, z, x, y)
        qxy = np.stack([quantise(xy[take, 0], tx, side), quantise(xy[take, 1], ty, side)], axis=1)
        sort = np.argsort(morton(qxy[:, 0], qxy[:, 1]), kind="stable")
        yield z, x, y, take[sort], qxy[sort]
        if len(rest):
            # The split line is exactly the children's shared edge (same expression as tile_square).
            cx, cy = tile_square(bounds, z + 1, 2 * x + 1, 2 * y + 1)[:2]
            right, upper = xy[rest, 0] >= cx, xy[rest, 1] >= cy
            for dx in (0, 1):
                for dy in (0, 1):
                    sub = rest[(right == bool(dx)) & (upper == bool(dy))]  # masking keeps priority order
                    if len(sub):
                        stack.append((z + 1, 2 * x + dx, 2 * y + dy, sub))


def encode_categories(meta: pa.Table, cluster: np.ndarray) -> tuple[np.ndarray, list[dict]]:
    """Category codes per row (0 = no value, then 1.. by falling frequency) and the legend for each column."""
    n = meta.num_rows
    cats = np.zeros((n, CAT_BYTES), dtype=np.uint8)
    columns = []
    tokens = meta["token"].to_numpy(zero_copy_only=False)
    for ci, name in enumerate(COLUMNS):
        if name == "cluster":
            values, labels_in = cluster.astype(np.int64), None
        else:
            col = meta[name].cast(pa.string()).combine_chunks().dictionary_encode()
            labels_in = col.dictionary.to_pylist()
            values = pc.fill_null(col.indices, -1).to_numpy().astype(np.int64) + 1  # 0 = null
        counts = np.bincount(values, minlength=1)
        present = [v for v in np.argsort(-counts, kind="stable") if v != 0 and counts[v] > 0]
        if len(present) > 255:
            raise SystemExit(f"column {name} has {len(present)} categories; a tile stores at most 255")
        remap = np.zeros(len(counts), dtype=np.uint8)
        labels = ["(none)"]
        for code, v in enumerate(present, 1):
            remap[v] = code
            if labels_in is not None:
                labels.append(labels_in[v - 1])
            else:
                # Rows are in rank order, so a cluster's first rows are its most frequent tokens.
                first = np.flatnonzero(values == v)[:3]
                labels.append(f"c{v:02d}: " + " ".join(str(t) for t in tokens[first]))
        cats[:, ci] = remap[values]
        columns.append(
            {
                "name": name,
                "labels": labels,
                "counts": np.bincount(cats[:, ci], minlength=len(labels)).tolist(),
            }
        )
    return cats, columns


def load_variant(site: Path, manifest: dict, name: str) -> dict[str, np.ndarray]:
    """Decode every tile of a variant back into flat arrays. The reference API and the tests use this."""
    v = next(v for v in manifest["variants"] if v["name"] == name)
    bounds = tuple(v["bounds"])
    ids, xs, ys, cats = [], [], [], []
    for z, x, y, n in v["tiles"]:
        i, q, c = decode_tile((site / "data" / v["tiles_path"] / f"{z}-{x}-{y}.bin").read_bytes())
        if len(i) != n:
            raise ValueError(f"tile {z}-{x}-{y} has {len(i)} points, manifest says {n}")
        tx, ty, side = tile_square(bounds, z, x, y)  # type: ignore[arg-type]
        ids.append(i)
        xs.append(dequantise(q[:, 0], tx, side))
        ys.append(dequantise(q[:, 1], ty, side))
        cats.append(c)
    if not ids:
        return {
            "id": np.zeros(0, np.uint32),
            "x": np.zeros(0, np.float32),
            "y": np.zeros(0, np.float32),
            "cats": np.zeros((0, CAT_BYTES), np.uint8),
        }
    return {
        "id": np.concatenate(ids),
        "x": np.concatenate(xs),
        "y": np.concatenate(ys),
        "cats": np.concatenate(cats),
    }


def run(s: Settings, source: str) -> dict:
    work = s.work(source)
    ingest = read_json(work / "ingest.json")
    src = read_json(s.raw(source) / "SOURCE.json")
    proj_info = {name: read_json(work / "proj" / f"{name}.json") for name in s.variants}
    evaluation = read_json(work / "eval.json") if (work / "eval.json").exists() else {"variants": {}}
    identity = {
        "format": FORMAT,
        "capacity": s.tile_capacity,
        "raw": ingest["raw"],
        "proj": {k: v["key"] for k, v in proj_info.items()},
    }
    build_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    data = s.site_dir / "data"
    out = data / build_id
    t0 = time.perf_counter()

    meta = pq.read_table(work / "meta.parquet")
    cluster = np.load(work / "cluster.npy")
    cats, columns = encode_categories(meta, cluster)

    if not (out / "DONE").exists():
        tmp = data / f"{build_id}.part"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        cluster_labels = pa.array(columns[2]["labels"])
        browser_meta = meta.drop_columns(["source_file"]).append_column(
            "cluster", pc.take(cluster_labels, pa.array(cats[:, 2])).dictionary_encode()
        )
        pq.write_table(
            browser_meta,
            tmp / "meta.parquet",
            row_group_size=ROW_GROUP,
            compression="zstd",
            compression_level=9,
        )
        tiles_meta = {}
        for name in s.variants:
            xy = np.load(work / "proj" / f"{name}.npy")
            ok = np.isfinite(xy).all(axis=1)
            rows = np.flatnonzero(
                ok
            )  # a projection that produced NaN for a row loses that row, counted below
            pts = xy[rows]
            (tmp / name).mkdir()
            bounds = square_bounds(pts) if len(pts) else (0.0, 0.0, 1.0)
            tiles = []
            total_bytes = 0
            for z, x, y, idx, qxy in build_tiles(pts, s.tile_capacity, s.seed, bounds):
                buf = encode_tile(rows[idx], qxy, cats[rows[idx]])
                (tmp / name / f"{z}-{x}-{y}.bin").write_bytes(buf)
                tiles.append([z, x, y, len(idx)])
                total_bytes += len(buf)
            tiles.sort()
            gx = np.clip(((pts[:, 0] - bounds[0]) / bounds[2] * GRID).astype(np.int64), 0, GRID - 1)
            gy = np.clip(((pts[:, 1] - bounds[1]) / bounds[2] * GRID).astype(np.int64), 0, GRID - 1)
            np.bincount(gy * GRID + gx, minlength=GRID * GRID).astype("<u4").tofile(
                tmp / name / "density.u32"
            )
            lo, hi = (
                np.percentile(pts, [0.5, 99.5], axis=0) if len(pts) else np.array([[0, 0], [1, 1]])
            ).tolist()
            tiles_meta[name] = {
                "bounds": list(bounds),
                "view": [lo[0], lo[1], hi[0], hi[1]],
                "tiles": tiles,
                "n_points": int(len(pts)),
                "dropped_non_finite": int((~ok).sum()),
                "bytes": total_bytes,
            }
        write_json(tmp / "tiles.json", tiles_meta)
        (tmp / "DONE").write_text("ok", encoding="utf-8")
        shutil.rmtree(out, ignore_errors=True)
        tmp.replace(out)
    tiles_meta = read_json(out / "tiles.json")

    variants = []
    for name in s.variants:
        params = parse_variant(name)
        info = proj_info[name]
        variants.append(
            {
                "name": name,
                "label": "PCA (linear baseline)"
                if name == "pca"
                else f"UMAP · {params['n_neighbors']} neighbours · min_dist {params['min_dist']}",
                "params": params,
                "tiles_path": f"{build_id}/{name}",
                "density_grid": GRID,
                "build_seconds": info["seconds"],
                "metrics": evaluation["variants"].get(name),
                **tiles_meta[name],
            }
        )
    manifest = {
        "format": FORMAT,
        "build_id": build_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {
            "name": source,
            "synthetic": src.get("url") is None,
            "url": src.get("url"),
            "sha256": [f["sha256"] for f in src["files"]],
            "lines": ingest["lines"],
            "accepted": ingest["accepted"],
            "quarantined": ingest["quarantined"],
            "quarantine_reasons": ingest["quarantine_reasons"],
        },
        "n_points": ingest["accepted"],
        "dim": ingest["dim"],
        "tile_capacity": s.tile_capacity,
        "columns": columns,
        "meta": {
            "path": f"{build_id}/meta.parquet",
            "bytes": (out / "meta.parquet").stat().st_size,
            "rows": meta.num_rows,
        },
        "eval": {k: v for k, v in evaluation.items() if k != "variants"},
        "variants": variants,
    }
    from .ops import remember_previous  # here, not at the top: ops imports this module

    remember_previous(data, build_id)
    write_json(
        data / "manifest.json", manifest
    )  # the only mutable file: everything under <build_id>/ is immutable
    event(
        "tiled",
        build_id=build_id,
        variants=len(variants),
        tiles=sum(len(v["tiles"]) for v in variants),
        seconds=round(time.perf_counter() - t0, 1),
        bytes=sum(v["bytes"] for v in variants),
    )
    return manifest
