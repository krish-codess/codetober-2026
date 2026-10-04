"""Offline corpus for experiments: the same feed, validation and taxonomy code as the service,
materialised as numpy arrays instead of database rows."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .embed import Embedder, Vecs, normalize_text
from .feed import inject_defects, mabsa_records, synthetic_records, write_feed
from .fetch import DOMAINS, fetch_mabsa
from .ingest import Accepted, gold_paths, validate
from .log import event
from .taxonomy import TAXONOMY_FILE, Bools, Tree, build_paths, canonical_path

logger = logging.getLogger(__name__)
FEED_NOW = datetime(2026, 10, 1, tzinfo=UTC)  # fixed "now" so validation of the seed feed is reproducible


class CachedEmbedder:
    """Content-addressed cache in front of an embedder: sha256(normalised text) -> vector.
    Makes the embedding stage idempotent and lets experiments and the database seed share work."""

    def __init__(self, inner: Embedder, path: Path, chunk: int = 4096) -> None:
        self.inner, self.path, self.chunk = inner, path, chunk
        self.rows: dict[bytes, int] = {}
        self.vecs: Vecs = np.empty((0, 384), dtype=np.float32)
        if path.exists():
            z = np.load(path, allow_pickle=False)
            self.vecs = z["vecs"]
            self.rows = {bytes(k): i for i, k in enumerate(z["keys"])}

    @staticmethod
    def _key(text: str) -> bytes:
        return hashlib.sha256(normalize_text(text).encode()).digest()

    def embed(self, texts: list[str]) -> Vecs:
        keys = [self._key(t) for t in texts]
        missing = list({k: t for k, t in zip(keys, texts, strict=True) if k not in self.rows}.items())
        for start in range(0, len(missing), self.chunk):
            part = missing[start : start + self.chunk]
            new = self.inner.embed([t for _, t in part])
            for k, _ in part:
                self.rows[k] = len(self.rows)
            self.vecs = np.concatenate([self.vecs, new])
            self._save()
            event(logger, "embed_progress", done=min(start + self.chunk, len(missing)), total=len(missing))
        return self.vecs[[self.rows[k] for k in keys]]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = b"".join(k for k, _ in sorted(self.rows.items(), key=lambda kv: kv[1]))
        keys = np.frombuffer(ordered, dtype=np.uint8).reshape(-1, 32)
        tmp = self.path.with_suffix(".tmp.npz")
        np.savez(tmp, keys=keys, vecs=self.vecs)
        tmp.replace(self.path)


def feed_path(data_dir: Path, source: str, test_langs: list[str], seed: int) -> Path:
    tag = hashlib.sha256(f"{source}|{','.join(test_langs)}|{seed}".encode()).hexdigest()[:10]
    return data_dir / "feeds" / f"{source}-{tag}.jsonl"


def build_feed(data_dir: Path, source: str, test_langs: list[str], seed: int = 0) -> Path:
    """Stage 1: produce the immutable feed file. No-op if it already exists."""
    path = feed_path(data_dir, source, test_langs, seed)
    if path.exists():
        return path
    if source == "real":
        fetch_mabsa(data_dir, set(test_langs) | {"en"} | set(_pool_langs()))
        records = mabsa_records(data_dir, test_langs, seed)
    else:
        records = synthetic_records(n_pool=1500, n_test=300, seed=seed)
    return write_feed(inject_defects(records, seed), path)


def _pool_langs() -> list[str]:
    from .feed import POOL_LANG_WEIGHTS

    return list(POOL_LANG_WEIGHTS)


def read_accepted(feed: Path) -> list[Accepted]:
    """Validate + de-duplicate exactly as `ingest_file` does, without a database."""
    out: list[Accepted] = []
    ids: set[tuple[str, str]] = set()
    pool_texts: set[str] = set()
    with feed.open("rb") as f:
        for line in f:
            rec = validate(line.rstrip(b"\r\n"), "feed", FEED_NOW)
            if not isinstance(rec, Accepted) or (rec.source, rec.external_id) in ids:
                continue
            ids.add((rec.source, rec.external_id))
            if rec.split == "pool":
                if rec.text_sha256 in pool_texts:
                    continue
                pool_texts.add(rec.text_sha256)
            out.append(rec)
    # leakage guard, as in ingest_file: drop pool texts that also occur in the evaluation split
    test_texts = {r.text_sha256 for r in out if r.split == "test"}
    return [r for r in out if r.split == "test" or r.text_sha256 not in test_texts]


def derive_taxonomy(records: list[Accepted]) -> list[str]:
    """Candidate taxonomy from the pool's reference labels (never the test split)."""
    counts: Counter[tuple[str, ...]] = Counter()
    for r in records:
        if r.split == "pool" and r.gold:
            for cat in r.gold[1]:
                path = canonical_path(r.gold[0], cat)
                if path:
                    counts[path] += 1
    domains = sorted({r.gold[0] for r in records if r.gold})
    return build_paths(counts, domains)


def load_taxonomy_paths(source: str, records: list[Accepted]) -> list[str]:
    """The committed, reviewed taxonomy for real data; derived on the fly for the synthetic feed."""
    if source == "real":
        return [n["path"] for n in json.loads(TAXONOMY_FILE.read_text(encoding="utf-8"))["nodes"]]
    return derive_taxonomy(records)


@dataclass(frozen=True)
class Split:
    x: Vecs
    y: Bools
    lang: NDArray[Any]
    group: NDArray[Any]
    text: list[str]

    def __len__(self) -> int:
        return len(self.text)

    def take(self, idx: NDArray[Any]) -> Split:
        return Split(self.x[idx], self.y[idx], self.lang[idx], self.group[idx], [self.text[i] for i in idx])


@dataclass(frozen=True)
class Corpus:
    tree: Tree
    paths: list[str]  # paths[j] is the path of tree position j
    pool: Split
    test: Split


def tree_from_paths(paths: list[str]) -> tuple[Tree, list[str], dict[str, int]]:
    ids = {p: i for i, p in enumerate(sorted(paths))}
    tree = Tree.from_edges((i, ids.get(p.rpartition("/")[0]) if "/" in p else None) for p, i in ids.items())
    by_id = {i: p for p, i in ids.items()}
    return tree, [by_id[int(n)] for n in tree.node_ids], ids


def load_corpus(
    data_dir: Path, embedder: Embedder, source: str = "real", test_langs: list[str] | None = None, seed: int = 0
) -> Corpus:
    test_langs = test_langs or _pool_langs()
    records = read_accepted(build_feed(data_dir, source, test_langs, seed))
    tree, paths, ids = tree_from_paths(load_taxonomy_paths(source, records))
    vecs = embedder.embed([r.text for r in records])

    def split(name: str) -> Split:
        idx = [i for i, r in enumerate(records) if r.split == name and r.gold is not None]
        y = tree.encode([gold_paths(records[i].gold, ids)[0] for i in idx])  # type: ignore[arg-type]
        return Split(
            vecs[idx], y, np.array([records[i].lang or "und" for i in idx]),
            np.array([records[i].group_key for i in idx]), [records[i].text for i in idx],
        )  # fmt: skip

    return Corpus(tree, paths, split("pool"), split("test"))


__all__ = ["DOMAINS", "CachedEmbedder", "Corpus", "Split", "build_feed", "load_corpus", "read_accepted"]
