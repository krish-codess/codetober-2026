"""Dataset boundary: scan raw files, validate, quarantine, split by provenance, cache as uint8.

The raw directory is never modified. Everything here is derived from it and can be rebuilt:
`python -m squeeze data <raw_dir>` is idempotent and prints the profile.

Why the split ignores the dataset's own train/ and val/ folders: Imagenette was re-split 70/30 from
ImageNet, so its val/ folder is mostly ImageNet *training* images (3,791 of 3,925) and its train/
folder holds 366 ImageNet *validation* images. Every model here starts from ImageNet weights, so
the only images no model has seen are the ones whose filename says they came from the ImageNet
validation set. Those, and only those, are the evaluation set.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

# (wnid, name, ImageNet-1k class index)
CLASSES: list[tuple[str, str, int]] = [
    ("n01440764", "tench", 0),
    ("n02102040", "English springer", 217),
    ("n02979186", "cassette player", 482),
    ("n03000684", "chain saw", 491),
    ("n03028079", "church", 497),
    ("n03394916", "French horn", 566),
    ("n03417042", "garbage truck", 569),
    ("n03425413", "gas pump", 571),
    ("n03445777", "golf ball", 574),
    ("n03888257", "parachute", 701),
]
WNID = {w: i for i, (w, _, _) in enumerate(CLASSES)}
IMAGE_SUFFIXES = {".jpeg", ".jpg", ".png"}
EVAL_PREFIX = "ILSVRC2012_val_"
MIN_SIDE = 32
SPLITS = ("eval", "dev", "calib", "train")


def split_of(name: str) -> str:
    """Provenance decides eval; a hash of the filename decides the rest, so adding files later
    never moves an existing file to another split."""
    if name.startswith(EVAL_PREFIX):
        return "eval"
    h = int(hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest()[:8], 16) / 2**32
    return "dev" if h < 0.08 else "calib" if h < 0.12 else "train"


def validate(path: Path, raw: bytes, size: int) -> tuple[np.ndarray | None, str | None, list[str]]:
    """Returns (pixels, quarantine_reason, flags). Exactly one of pixels / reason is set."""
    flags: list[str] = []
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return None, "not_an_image", flags
    wnid = path.parent.name
    if wnid not in WNID:
        return None, "unknown_class", flags
    stem_wnid = path.name.split("_")[0]
    if stem_wnid in WNID and stem_wnid != wnid:
        return None, f"label_conflict:{stem_wnid}", flags
    if not raw:
        return None, "empty", flags
    try:
        im: Image.Image = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as exc:  # PIL raises many unrelated types for damaged files
        return None, f"undecodable:{type(exc).__name__}", flags
    w, h = im.size
    if min(w, h) < MIN_SIDE:
        return None, "too_small", flags
    if im.mode != "RGB":
        flags.append(f"converted:{im.mode}")
        im = im.convert("RGB")
    if max(w, h) > 3 * min(w, h):
        flags.append("aspect>3")
    if min(w, h) != size:
        flags.append("resized")
        scale = size / min(w, h)
        im = im.resize((max(size, round(w * scale)), max(size, round(h * scale))), Image.Resampling.BILINEAR)
        w, h = im.size
    left, top = (w - size) // 2, (h - size) // 2
    return np.asarray(im.crop((left, top, left + size, top + size)), dtype=np.uint8), None, flags


@dataclass
class Dataset:
    images: np.ndarray  # uint8, N x size x size x 3
    labels: np.ndarray  # int64
    splits: np.ndarray  # str
    names: list[str]
    version: str
    profile: dict[str, Any]

    def idx(self, split: str, limit: int | None = None) -> np.ndarray:
        """Indices of a split in filename order (stable), optionally the first `limit`."""
        i = np.flatnonzero(self.splits == split)
        return i if limit is None else i[:limit]


def build(raw_root: Path, out_dir: Path, size: int = 160) -> Dataset:
    """Scan, validate and cache. Re-running on an unchanged raw directory reuses the cache."""
    files = sorted(p for p in raw_root.rglob("*") if p.is_file())
    listing = hashlib.sha256()
    for p in files:
        listing.update(f"{p.relative_to(raw_root).as_posix()}:{p.stat().st_size}\n".encode())
    stamp = f"{size}:{listing.hexdigest()}"
    meta_path = out_dir / "index.json"
    if meta_path.exists() and json.loads(meta_path.read_text())["stamp"] == stamp:
        return load(out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    kept: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    with (out_dir / "images.u8").open("wb") as sink:  # streamed: the full set does not fit in RAM here
        for p in files:
            rel = p.relative_to(raw_root).as_posix()
            raw = p.read_bytes()
            arr, reason, flags = validate(p, raw, size)
            digest = hashlib.sha1(raw, usedforsecurity=False).hexdigest()
            if arr is not None and digest in seen:
                arr, reason = None, f"duplicate_of:{seen[digest]}"
            if arr is None:
                quarantine.append({"path": rel, "reason": reason, "bytes": len(raw)})
                continue
            seen[digest] = rel
            kept.append(
                {
                    "path": rel,
                    "name": p.name,
                    "label": WNID[p.parent.name],
                    "split": split_of(p.name),
                    "sha1": digest,
                    "flags": flags,
                }
            )
            sink.write(arr.tobytes())

    version = hashlib.sha256(
        "\n".join(f"{k['sha1']}:{k['label']}:{k['split']}" for k in kept).encode()
    ).hexdigest()[:16]
    profile = _profile(kept, quarantine, size)
    (out_dir / "quarantine.jsonl").write_text("".join(json.dumps(q) + "\n" for q in quarantine))
    (out_dir / "profile.json").write_text(json.dumps(profile, indent=2))
    meta_path.write_text(json.dumps({"stamp": stamp, "version": version, "size": size, "items": kept}))
    return load(out_dir)


def _profile(kept: list[dict[str, Any]], quarantine: list[dict[str, Any]], size: int) -> dict[str, Any]:
    def count(rows: list[dict[str, Any]], key: Any) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rows:
            for k in key(r):
                out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items()))

    return {
        "size": size,
        "kept": len(kept),
        "quarantined": len(quarantine),
        "quarantine_reasons": count(quarantine, lambda q: [q["reason"].split(":")[0]]),
        "by_split": count(kept, lambda k: [k["split"]]),
        "by_split_and_class": count(kept, lambda k: [f"{k['split']}/{CLASSES[k['label']][1]}"]),
        "flags": count(
            kept,
            lambda k: [f.split(":")[0] + (":" + f.split(":")[1] if ":" in f else "") for f in k["flags"]],
        ),
        # What the dataset's own folders would have leaked: ImageNet-train images sitting in val/,
        # and ImageNet-val images sitting in train/.
        "folder_vs_provenance": count(
            kept,
            lambda k: [
                f"{k['path'].split('/')[0]}/ holds imagenet-{'val' if k['split'] == 'eval' else 'train'}"
            ],
        ),
    }


def load(out_dir: Path) -> Dataset:
    meta = json.loads((out_dir / "index.json").read_text())
    items, size = meta["items"], meta["size"]
    return Dataset(
        images=np.memmap(out_dir / "images.u8", dtype=np.uint8, mode="r", shape=(len(items), size, size, 3)),
        labels=np.array([k["label"] for k in items], dtype=np.int64),
        splits=np.array([k["split"] for k in items]),
        names=[k["name"] for k in items],
        version=meta["version"],
        profile=json.loads((out_dir / "profile.json").read_text()),
    )


def deploy_conditions(images: np.ndarray, seed: int = 0) -> np.ndarray:
    """What a cheap camera module does to a picture: low light, sensor noise, soft focus, and a
    hard JPEG. Seeded per image, so the same input always gives the same output."""
    out = np.empty_like(images)
    for i, img in enumerate(images):
        rng = np.random.default_rng([seed, i])
        x = (img.astype(np.float32) / 255.0) ** rng.uniform(1.2, 2.0)  # darken
        if rng.random() < 0.5:  # 3x3 box blur
            p = np.pad(x, ((1, 1), (1, 1), (0, 0)), mode="edge")
            h, w = x.shape[:2]
            x = sum(p[a : a + h, b : b + w] for a in range(3) for b in range(3)) / 9.0
        x = x + rng.normal(0.0, rng.uniform(4, 12) / 255.0, x.shape)
        u8 = (np.clip(x, 0, 1) * 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(u8).save(buf, "JPEG", quality=int(rng.integers(25, 60)))
        out[i] = np.asarray(Image.open(buf).convert("RGB"))
    return out
