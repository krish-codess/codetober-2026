"""Stage 2: validate raw text at the boundary, land vectors + metadata, quarantine the rest.

Reads every raw/<source>/*.txt in name order. Every input line ends up in exactly one place:
`vectors.f32` + `meta.parquet` (accepted) or `quarantine.parquet` (rejected, with a reason).
Ids are assigned in order of acceptance, so a file that arrives later (part-0002.txt) never
renumbers earlier rows.
"""

from __future__ import annotations

import time
import unicodedata
from collections import Counter
from functools import cache
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .config import Settings, event, read_json, write_json

# Row groups of 64k rows: a point lookup by id touches one group (min/max statistics on the sorted
# id column prune the rest), and DuckDB-WASM scans groups in parallel.
ROW_GROUP = 65536

SCRIPTS = (
    "Latin",
    "Cyrillic",
    "Arabic",
    "Kana",
    "Hangul",
    "Han",
    "Thai",
    "Greek",
    "Hebrew",
    "Devanagari",
    "Other",
)
KINDS = ("word", "hashtag", "mention", "number", "symbol", "placeholder", "mixed")
_NAME_TO_SCRIPT = {
    "LATIN": "Latin", "CYRILLIC": "Cyrillic", "ARABIC": "Arabic", "HIRAGANA": "Kana", "KATAKANA": "Kana",
    "HANGUL": "Hangul", "CJK": "Han", "THAI": "Thai", "GREEK": "Greek", "HEBREW": "Hebrew", "DEVANAGARI": "Devanagari",
}  # fmt: skip


@cache
def _char_script(ch: str) -> str:
    try:
        return _NAME_TO_SCRIPT.get(unicodedata.name(ch).split(" ")[0].split("-")[0], "Other")
    except ValueError:  # unnamed code point
        return "Other"


def script_of(token: str) -> str | None:
    """Script of the majority of the token's letters; None when it has no letters at all."""
    counts = Counter(_char_script(c) for c in token if c.isalpha())
    return counts.most_common(1)[0][0] if counts else None


def kind_of(token: str) -> str:
    if len(token) > 2 and token[0] == "<" and token[-1] == ">":
        return "placeholder"  # <user>, <url>, <number>: inserted by the GloVe tweet preprocessor
    if token[0] == "#" and len(token) > 1:
        return "hashtag"
    if token[0] == "@" and len(token) > 1:
        return "mention"
    if not any(c.isalnum() for c in token):
        return "symbol"
    if all(c.isdigit() or c in ".,:-/%" for c in token):
        return "number"
    if all(c.isalpha() or c in "'-_" for c in token):
        return "word"
    return "mixed"


def parse_line(raw: bytes, dim: int) -> tuple[str, np.ndarray] | str:
    """Return (token, vector) or the reason the line is rejected."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "bad_encoding"
    # Split from the right: a token may itself contain spaces or be an odd whitespace character.
    parts = text.rstrip("\r\n").rsplit(" ", dim)
    if len(parts) != dim + 1:
        return "wrong_field_count"
    token = parts[0]
    if not token or token.isspace():
        return "empty_token"
    try:
        vec = np.array(parts[1:], dtype=np.float32)
    except ValueError:
        return "bad_number"
    if not np.isfinite(vec).all():
        return "non_finite"
    if not vec.any():
        return "zero_vector"  # no direction, so no cosine neighbours
    return token, vec


def run(s: Settings, source: str, force: bool = False) -> dict:
    raw, work = s.raw(source), s.work(source)
    src = read_json(raw / "SOURCE.json")
    dim = int(src["dim"])
    out = work / "ingest.json"
    if out.exists() and not force and read_json(out)["raw"] == src["files"]:
        event("ingest skipped, outputs match raw", source=source)
        return read_json(out)

    t0 = time.perf_counter()
    work.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    tokens: list[str] = []
    ranks: list[int] = []
    files: list[int] = []
    quarantine: list[tuple[int, int, str, str]] = []  # file index, line, reason, first bytes
    lines = 0
    tmp_vec = work / "vectors.f32.part"
    with tmp_vec.open("wb") as fv:
        for fi, f in enumerate(src["files"]):
            with (raw / f["name"]).open("rb") as fh:
                for line_no, line in enumerate(fh, 1):
                    lines += 1
                    parsed = parse_line(line, dim)
                    if not isinstance(parsed, str) and parsed[0] in seen:
                        parsed = "duplicate_token"  # first occurrence wins
                    if isinstance(parsed, str):
                        quarantine.append((fi, line_no, parsed, line[:80].decode("utf-8", "replace")))
                        continue
                    token, vec = parsed
                    seen.add(token)
                    tokens.append(token)
                    ranks.append(line_no)
                    files.append(fi)
                    fv.write(vec.tobytes())
    n = len(tokens)
    if n == 0:
        raise SystemExit(
            f"{source}: no valid rows in {lines} lines; see the reasons above"
        )  # nothing to project
    tmp_vec.replace(work / "vectors.f32")

    scripts = [script_of(t) for t in tokens]
    kinds = [kind_of(t) for t in tokens]
    meta = pa.table(
        {
            "id": pa.array(np.arange(n, dtype=np.uint32)),
            "token": pa.array(tokens, pa.string()),
            "rank": pa.array(ranks, pa.uint32()),
            "source_file": pa.array(files, pa.uint8()),
            "n_chars": pa.array([len(t) for t in tokens], pa.uint16()),
            "script": pa.array(scripts, pa.string()).dictionary_encode(),
            "kind": pa.array(kinds, pa.string()).dictionary_encode(),
        }
    )
    pq.write_table(meta, work / "meta.parquet", row_group_size=ROW_GROUP, compression="zstd")
    pq.write_table(
        pa.table(
            {
                "source_file": pa.array([q[0] for q in quarantine], pa.uint8()),
                "line": pa.array([q[1] for q in quarantine], pa.uint32()),
                "reason": pa.array([q[2] for q in quarantine], pa.string()),
                "head": pa.array([q[3] for q in quarantine], pa.string()),
            }
        ),
        work / "quarantine.parquet",
    )

    secs = time.perf_counter() - t0
    reasons = dict(Counter(q[2] for q in quarantine))
    assert n + len(quarantine) == lines, "every line must be accepted or quarantined"
    report = {
        "source": source,
        "raw": src["files"],
        "dim": dim,
        "lines": lines,
        "accepted": n,
        "quarantined": len(quarantine),
        "quarantine_reasons": reasons,
        "seconds": round(secs, 1),
        "rows_per_s": round(lines / secs),
    }
    write_json(out, report)
    write_json(work / "profile.json", profile(work, n, dim, scripts, kinds, tokens, reasons, lines))
    event("ingested", **{k: v for k, v in report.items() if k != "raw"})
    return report


def profile(
    work: Path, n: int, dim: int, scripts: list, kinds: list, tokens: list, reasons: dict, lines: int
) -> dict:
    """What the data actually looks like: nulls, cardinality, distributions, assumption violations."""
    vec = np.memmap(work / "vectors.f32", dtype=np.float32, mode="r", shape=(n, dim))
    sample = np.asarray(vec[:: max(1, n // 200_000)])
    norms = np.linalg.norm(sample, axis=1)
    lengths = np.array([len(t) for t in tokens])
    q = [0, 1, 25, 50, 75, 99, 100]
    return {
        "rows": {"lines": lines, "accepted": n, "quarantined": lines - n, "quarantine_reasons": reasons},
        "token": {
            "distinct": len(set(tokens)),
            "n_chars_percentiles": dict(zip(map(str, q), np.percentile(lengths, q).tolist(), strict=True)),
            "with_whitespace": sum(1 for t in tokens if any(c.isspace() for c in t)),
            "non_ascii": sum(1 for t in tokens if not t.isascii()),
            "not_nfc_normalised": sum(1 for t in tokens if not unicodedata.is_normalized("NFC", t)),
            "case_insensitive_collisions": n - len({t.lower() for t in tokens}),
        },
        "script": {
            "null": scripts.count(None),
            "counts": dict(Counter(x for x in scripts if x).most_common()),
        },
        "kind": {"null": 0, "counts": dict(Counter(kinds).most_common())},
        "vector": {
            "dim": dim,
            "sampled_rows": len(sample),
            "norm_percentiles": dict(
                zip(map(str, q), np.percentile(norms, q).round(4).tolist(), strict=True)
            ),
            "value_min": float(sample.min()),
            "value_max": float(sample.max()),
            "per_dim_mean_abs_max": float(np.abs(sample.mean(axis=0)).max()),
            "per_dim_std_range": [float(sample.std(axis=0).min()), float(sample.std(axis=0).max())],
        },
    }
