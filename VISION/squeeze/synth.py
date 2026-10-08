"""Generators with the same shape and the same defects as the real inputs.

`dataset` writes a small image folder laid out like Imagenette, with every defect the validator is
meant to catch planted in it. `bench_results` (below) does the same for device result uploads.
Both feed the same code paths as the real sources; tests and CI run on them.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image

from .data import CLASSES, EVAL_PREFIX


def _image(rng: np.random.Generator, label: int, size: int) -> Image.Image:
    """Learnable but not trivial: the class sets the hue and stripe frequency, noise does the rest."""
    yy, xx = np.mgrid[0:size, 0:size] / size
    base = np.stack([np.sin((label + 1) * 3 * xx + c * 2.1 + label) for c in range(3)], -1) * 0.5 + 0.5
    noisy = base * 0.7 + rng.random((size, size, 3)) * 0.3 + yy[..., None] * 0.0
    return Image.fromarray((np.clip(noisy, 0, 1) * 255).astype(np.uint8))


def _jpeg(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def dataset(
    root: Path, seed: int = 0, per_class: int = 24, size: int = 64, defects: bool = True
) -> dict[str, str]:
    """Returns {relative path: expected quarantine reason or kept-flag} for every planted defect."""
    rng = np.random.default_rng(seed)
    for label, (wnid, _, _) in enumerate(CLASSES):
        for i in range(per_class):
            folder = "val" if i % 3 == 0 else "train"
            # A quarter of the files carry ImageNet-validation provenance, in both folders.
            name = f"{EVAL_PREFIX}{label:04d}{i:04d}.JPEG" if i % 4 == 0 else f"{wnid}_{i}.JPEG"
            path = root / folder / wnid / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_jpeg(_image(rng, label, size)))
    planted: dict[str, str] = {}
    if not defects:
        return planted
    a, b = CLASSES[0][0], CLASSES[1][0]
    good = _jpeg(_image(rng, 0, size))

    def plant(rel: str, raw: bytes, expect: str) -> None:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(raw)
        planted[rel] = expect

    plant(f"train/{a}/{a}_9001.JPEG", b"", "empty")
    plant(f"train/{a}/{a}_9002.JPEG", good[: len(good) // 3], "undecodable")
    plant(f"train/{a}/{a}_9003.JPEG", b"not a jpeg at all", "undecodable")
    plant(f"train/{a}/{a}_9004.JPEG", _jpeg(_image(rng, 0, 8)), "too_small")
    plant(f"train/{b}/{a}_9005.JPEG", good, "label_conflict")
    plant("train/n99999999/n99999999_1.JPEG", good, "unknown_class")
    plant("train/.DS_Store", b"\x00\x00\x00\x01Bud1", "not_an_image")
    plant("noisy_imagenette.csv", b"path,noisy_labels_0\n", "not_an_image")
    first = sorted((root / "train" / a).glob(f"{a}_*.JPEG"))[0]
    plant(f"val/{a}/{a}_9006.JPEG", first.read_bytes(), "duplicate_of")
    plant(f"train/{a}/{a}_9007.JPEG", _jpeg(_image(rng, 0, size).convert("L")), "converted:L")
    plant(f"train/{a}/{a}_9008.JPEG", _jpeg(_image(rng, 0, size).convert("CMYK")), "converted:CMYK")
    return planted
