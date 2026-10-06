"""The variant matrix: how each format is written, how it is read, and how reads are counted."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.fs as pafs
import pyarrow.orc as orc
import pyarrow.parquet as pq
from fsspec.implementations.local import LocalFileSystem

# Which library reads each format at query time. Every number in the results is a property of
# (format, reader), never of the format alone.
READERS = {"parquet": "DuckDB native", "csv": "DuckDB native", "orc": "PyArrow dataset -> DuckDB",
           "avro": "polars -> DuckDB"}
PARQUET_DEFAULT_ROW_GROUP = 1 << 20  # PyArrow's default, in rows
ORC_DEFAULT_STRIPE = 64 << 20        # PyArrow's default, in bytes


@dataclass(frozen=True)
class Variant:
    format: str                # parquet | orc | avro | csv
    codec: str                 # the writer's own name for it; "none" = uncompressed
    level: int | None = None   # compression level, where the codec has one. ORC has no levels, only a
                               # strategy: any level selects "compression" over the default "speed"
    chunk: int | None = None   # parquet: rows per row group; orc: stripe bytes; None = writer default
    layout: str = "sorted"     # sorted on the dataset sort key, or shuffled (the pushdown control)

    @property
    def id(self) -> str:
        level = "" if not self.level else "-best" if self.format == "orc" else str(self.level)
        parts = [self.format, self.codec + level]
        if self.chunk:
            parts.append(f"rg{self.chunk // 1000}k" if self.format == "parquet" else f"stripe{self.chunk >> 20}m")
        if self.layout != "sorted":
            parts.append(self.layout)
        return "-".join(parts)

    @property
    def filename(self) -> str:
        ext = {"gzip": ".gz", "zstd": ".zst"}.get(self.codec, "") if self.format == "csv" else ""
        return f"data.{self.format}{ext}"

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, **asdict(self), "reader": READERS[self.format]}


MATRIX: list[Variant] = [
    Variant("csv", "none"), Variant("csv", "gzip"), Variant("csv", "zstd"),
    # codec axis, at the writer's default row group
    Variant("parquet", "none"), Variant("parquet", "snappy"), Variant("parquet", "lz4"),
    Variant("parquet", "gzip"), Variant("parquet", "zstd", 3), Variant("parquet", "zstd", 19),
    # row-group axis, codec held at zstd-3
    Variant("parquet", "zstd", 3, 10_000), Variant("parquet", "zstd", 3, 100_000),
    Variant("parquet", "zstd", 3, 8_000_000),
    # layout control: same bytes of data, sort order destroyed
    Variant("parquet", "zstd", 3, layout="shuffled"),
    Variant("orc", "none"), Variant("orc", "snappy"), Variant("orc", "lz4"), Variant("orc", "zlib"),
    Variant("orc", "zstd"),
    # at the default "speed" strategy ORC's lz4 barely compresses; this is the same codec asked to try
    Variant("orc", "lz4", 1),
    # stripe axis, codec held at zstd
    Variant("orc", "zstd", chunk=8 << 20), Variant("orc", "zstd", chunk=256 << 20),
    Variant("avro", "none"), Variant("avro", "snappy"), Variant("avro", "deflate"),
]
# The column lab varies codec only: one file per (column, format, codec).
LAB_MATRIX = [v for v in MATRIX if v.chunk is None and v.layout == "sorted" and v.format != "csv"
              and (v.level is None or v.id == "parquet-zstd3")]


def shuffled(table: pa.Table, seed: int) -> pa.Table:
    return table.take(np.random.default_rng(seed).permutation(table.num_rows))


def write_variant(table: pa.Table, v: Variant, path: Path) -> None:
    """Encode `table` as variant `v`. Atomic: the final name only ever holds a complete file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("tmp-" + path.name)  # keeps the extension, which DuckDB reads the codec from
    if v.format == "parquet":
        pq.write_table(table, tmp, compression=v.codec, compression_level=v.level,
                       row_group_size=v.chunk or PARQUET_DEFAULT_ROW_GROUP)
    elif v.format == "orc":
        orc.write_table(table, str(tmp), compression="uncompressed" if v.codec == "none" else v.codec,
                        compression_strategy="compression" if v.level else "speed",
                        stripe_size=v.chunk or ORC_DEFAULT_STRIPE)
    elif v.format == "avro":
        frame = pl.from_arrow(table)
        assert isinstance(frame, pl.DataFrame)
        frame.write_avro(tmp, compression="uncompressed" if v.codec == "none" else v.codec)  # type: ignore[arg-type]
    elif v.format == "csv":
        codec: Any = None if v.codec == "none" else v.codec
        duckdb.connect().from_arrow(table).write_csv(str(tmp), header=True, compression=codec)
    else:
        raise ValueError(f"unknown format {v.format!r}")
    os.replace(tmp, path)


# ---------------------------------------------------------------- counting reads

class _CountingFile:
    def __init__(self, f: Any, fs: CountingFS) -> None:
        self._f, self._fs = f, fs

    def read(self, n: int = -1) -> bytes:
        data: bytes = self._f.read(n)
        self._fs.count(len(data))
        return data

    def readinto(self, b: Any) -> int:
        n: int = self._f.readinto(b)
        self._fs.count(n)
        return n

    def __getattr__(self, name: str) -> Any:
        return getattr(self._f, name)

    def __enter__(self) -> _CountingFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self._f.close()


class CountingFS(LocalFileSystem):  # type: ignore[misc]
    """A local filesystem that counts every byte and every read call handed to the reader.

    This is the storage-side view: on object storage `bytes` is what a scan-priced engine
    bills, and `reads` approximates the number of ranged GETs.
    """

    protocol = "counted"
    cachable = False  # fsspec caches instances by default; each measurement needs its own counters

    def __init__(self) -> None:
        super().__init__()
        self.bytes = self.reads = 0
        self._lock = threading.Lock()  # DuckDB reads from several threads

    def count(self, n: int) -> None:
        with self._lock:
            self.bytes += n
            self.reads += 1

    def _open(self, path: str, mode: str = "rb", **kwargs: Any) -> Any:
        return _CountingFile(super()._open(path, mode, **kwargs), self)


def open_source(con: duckdb.DuckDBPyConnection, v: Variant, path: Path, schema: list[list[str]],
                fs: CountingFS | None = None) -> None:
    """Bind variant `v` as relation `t` on `con`. With `fs`, every read goes through the counter.

    Parquet and CSV are scanned by DuckDB itself. ORC is scanned by PyArrow's dataset reader,
    which DuckDB pushes projections and filters into. Avro has no pushdown anywhere in this
    stack: polars decodes the whole file, and that decode is part of every query's time.
    """
    p = path.as_posix()
    if fs is not None and v.format in ("parquet", "csv"):
        if fs.protocol not in con.list_filesystems():
            con.register_filesystem(fs)
        con.execute("SET enable_external_file_cache = false")  # or a repeat query reads 0 bytes from 'storage'
        p = "counted://" + p
    if v.format == "parquet":
        con.read_parquet(p).create_view("t")
    elif v.format == "csv":
        # The schema is known, so skip DuckDB's type sniffer: it costs ~0.5 s per bind and would be
        # charged to every CSV query. This is CSV's best case.
        con.read_csv(p, header=True, columns={name: typ for name, typ in schema}, auto_detect=False).create_view("t")
    elif v.format == "orc":
        arrow_fs = pafs.PyFileSystem(pafs.FSSpecHandler(fs)) if fs is not None else None  # type: ignore[attr-defined]
        con.register("t", pads.dataset(p, format="orc", filesystem=arrow_fs))
    elif v.format == "avro":
        with (fs.open(p, "rb") if fs is not None else open(p, "rb")) as f:
            con.register("t", pl.read_avro(f).to_arrow())
    else:
        raise ValueError(f"unknown format {v.format!r}")


def close_source(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("DROP VIEW IF EXISTS t")
    con.unregister("t")


# ---------------------------------------------------------------- file anatomy

def anatomy(v: Variant, path: Path) -> dict[str, Any]:
    """Chunk count, and for Parquet the per-column sizes and encodings from the footer."""
    if v.format == "orc":
        return {"chunks": orc.ORCFile(str(path)).nstripes}
    if v.format != "parquet":
        return {"chunks": None}
    md = pq.ParquetFile(path).metadata
    cols: dict[str, dict[str, Any]] = {}
    for g in range(md.num_row_groups):
        for c in range(md.num_columns):
            chunk = md.row_group(g).column(c)
            col = cols.setdefault(chunk.path_in_schema, {"compressed": 0, "uncompressed": 0, "encodings": set()})
            col["compressed"] += chunk.total_compressed_size
            col["uncompressed"] += chunk.total_uncompressed_size
            col["encodings"].update(chunk.encodings)
    return {"chunks": md.num_row_groups,
            "columns": [{"column": k, "compressed": c["compressed"], "uncompressed": c["uncompressed"],
                         "encodings": sorted(c["encodings"])} for k, c in cols.items()]}


# ---------------------------------------------------------------- cold runs

def evict(path: Path) -> bool:
    """Ask the OS to drop `path` from its page cache. Returns False where there is no way to ask.

    Linux: posix_fadvise(DONTNEED), no privileges needed. Windows: opening a file with
    FILE_FLAG_NO_BUFFERING purges its cached pages on most configurations. Neither is a
    guarantee, which is why `eviction_check` measures whether it worked.
    """
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x20000000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            return False
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    if hasattr(os, "posix_fadvise"):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)  # type: ignore[attr-defined]
        finally:
            os.close(fd)
        return True
    return False


def _read_mb_s(path: Path) -> float:
    t0 = time.perf_counter()
    with open(path, "rb", buffering=0) as f:
        while f.read(8 << 20):
            pass
    return path.stat().st_size / 1e6 / max(time.perf_counter() - t0, 1e-9)


def eviction_check(path: Path) -> dict[str, Any]:
    """Evidence for or against 'cold': sequential read throughput, cached versus just evicted."""
    _read_mb_s(path)
    cached = max(_read_mb_s(path), _read_mb_s(path))
    supported = evict(path)
    evicted = _read_mb_s(path)
    return {"supported": supported, "file_mb": round(path.stat().st_size / 1e6, 1),
            "cached_mb_s": round(cached), "evicted_mb_s": round(evicted),
            # Cached reads are a memory copy. If the post-eviction read is not clearly slower,
            # the pages never left memory and 'cold' on this machine only means 'fresh engine'.
            "effective": supported and evicted < 0.6 * cached}
