"""Immutable, content-addressed store for raw source payloads.

Every byte we ever received is kept exactly as received, wrapped in an envelope that records
where and when it came from. Files are write-once: a second write of identical content is a
no-op (idempotent retries), and nothing in the codebase opens a raw file for writing twice.
Everything downstream can be rebuilt from this directory.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def _safe(part: str) -> str:
    if not part or any(c not in SAFE for c in part) or part.startswith("."):
        raise ValueError(f"unsafe path component: {part!r}")
    return part


@dataclass(frozen=True)
class RawRef:
    path: Path
    sha256: str
    source: str
    kind: str
    day: date
    key: str


@dataclass(frozen=True)
class RawRecord:
    ref: RawRef
    fetched_at: datetime
    observed_at: datetime
    meta: dict[str, Any]
    body: str


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, source: str, kind: str, day: date) -> Path:
        return self.root / _safe(source) / _safe(kind) / f"day={day.isoformat()}"

    def put(
        self,
        *,
        source: str,
        kind: str,
        day: date,
        key: str,
        body: str,
        fetched_at: datetime,
        observed_at: datetime,
        meta: dict[str, Any] | None = None,
    ) -> RawRef:
        envelope = {
            "source": source,
            "kind": kind,
            "key": key,
            "fetched_at": fetched_at.isoformat(),
            "observed_at": observed_at.isoformat(),
            "meta": meta or {},
            "body": body,
        }
        data = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        sha = hashlib.sha256(data).hexdigest()
        d = self._dir(source, kind, day)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_safe(key)}__{sha[:20]}.json.gz"
        ref = RawRef(path, sha, source, kind, day, key)
        if path.exists():
            return ref  # identical content already stored: retry-safe no-op
        # mtime=0 makes the gzip bytes deterministic; tmp + atomic rename means readers
        # never observe a half-written file even if we crash mid-write.
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
                gz.write(data)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        os.chmod(path, 0o444)
        return ref

    def iter_refs(self, source: str, kind: str, day: date) -> Iterator[RawRef]:
        d = self._dir(source, kind, day)
        if not d.exists():
            return
        for p in sorted(d.glob("*.json.gz")):
            key, _, short = p.name.removesuffix(".json.gz").rpartition("__")
            yield RawRef(p, short, source, kind, day, key)

    def days(self, source: str, kind: str) -> list[date]:
        d = self.root / _safe(source) / _safe(kind)
        if not d.exists():
            return []
        return sorted(date.fromisoformat(p.name.removeprefix("day=")) for p in d.glob("day=*"))

    def read(self, ref: RawRef) -> RawRecord:
        with gzip.open(ref.path, "rb") as gz:
            data = gz.read()
        sha = hashlib.sha256(data).hexdigest()
        if not sha.startswith(ref.sha256):
            raise ValueError(f"raw file corrupted: {ref.path} (hash mismatch)")
        env = json.loads(data)
        return RawRecord(
            ref=RawRef(ref.path, sha, ref.source, ref.kind, ref.day, ref.key),
            fetched_at=datetime.fromisoformat(env["fetched_at"]),
            observed_at=datetime.fromisoformat(env["observed_at"]),
            meta=env["meta"],
            body=env["body"],
        )
