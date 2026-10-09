from __future__ import annotations

import dataclasses

import numpy as np
import pyarrow.parquet as pq
import pytest

from dots import fetch, ingest
from dots.config import read_json, write_json
from dots.fetch import write_synth
from dots.ingest import kind_of, parse_line, script_of

from .conftest import make_settings

VEC = " ".join(["0.5"] * 3)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"hello {VEC}\n".encode(), "hello"),
        (f"new york {VEC}\n".encode(), "new york"),  # a token may contain spaces: split from the right
        (f"привет {VEC}\r\n".encode(), "привет"),
        (b"\xff\xfe" + f"x {VEC}\n".encode(), "bad_encoding"),
        (b"x 0.5 0.5\n", "wrong_field_count"),
        (f" {VEC}\n".encode(), "empty_token"),
        (
            f"\u0085 {VEC}\n".encode(),
            "empty_token",
        ),  # the real file has this: a token that is one control character
        (b"x 0.5 abc 0.5\n", "bad_number"),
        (b"x 0.5 nan 0.5\n", "non_finite"),
        (b"x inf 0.5 0.5\n", "non_finite"),
        (b"x 0 0.0 -0\n", "zero_vector"),
    ],
)
def test_parse_line(line, expected):
    got = parse_line(line, 3)
    assert (got if isinstance(got, str) else got[0]) == expected


@pytest.mark.parametrize(
    ("token", "script", "kind"),
    [
        ("hello", "Latin", "word"),
        ("don't", "Latin", "word"),
        ("привет", "Cyrillic", "word"),
        ("こんにちは", "Kana", "word"),
        ("#монетка", "Cyrillic", "hashtag"),
        ("<user>", "Latin", "placeholder"),
        ("12:30", None, "number"),
        ("!!!", None, "symbol"),
        ("abc123", "Latin", "mixed"),
        ("@", None, "symbol"),
    ],
)
def test_derived_columns(token, script, kind):
    assert script_of(token) == script
    assert kind_of(token) == kind


def test_every_planted_defect_is_quarantined_with_its_reason(built):
    s, source, _ = built
    planted = read_json(s.raw(source) / "SOURCE.json")["planted_defects"]
    report = read_json(s.work(source) / "ingest.json")
    assert all(n > 0 for n in planted.values()), "the generator must plant every kind of defect"
    assert report["quarantine_reasons"] == planted
    assert report["accepted"] + report["quarantined"] == report["lines"] == s.synth_rows
    q = pq.read_table(s.work(source) / "quarantine.parquet")
    assert q.num_rows == report["quarantined"]
    assert set(q["reason"].to_pylist()) == set(planted)


def test_landed_data_is_consistent(built):
    s, source, _ = built
    work = s.work(source)
    report = read_json(work / "ingest.json")
    meta = pq.read_table(work / "meta.parquet")
    n = report["accepted"]
    assert meta.num_rows == n
    assert meta["id"].to_pylist() == list(range(n))
    assert len(set(meta["token"].to_pylist())) == n
    vec = np.memmap(work / "vectors.f32", dtype=np.float32, mode="r", shape=(n, report["dim"]))
    assert np.isfinite(vec).all() and vec.any(axis=1).all()
    assert pq.ParquetFile(work / "meta.parquet").metadata.row_group(0).num_rows <= ingest.ROW_GROUP
    assert meta["script"].null_count > 0, "tokens without letters have no script; the column is nullable"


def test_ingest_is_idempotent_and_late_files_keep_ids_stable(tmp_path):
    s = make_settings(tmp_path, synth_rows=600)
    source = fetch.run(s)
    first = ingest.run(s, source)
    stamp = (s.work(source) / "vectors.f32").stat().st_mtime_ns
    assert ingest.run(s, source) == first
    assert (s.work(source) / "vectors.f32").stat().st_mtime_ns == stamp, (
        "a second run must not rewrite anything"
    )
    before = pq.read_table(s.work(source) / "meta.parquet").to_pydict()

    # A second file arrives later, overlapping the first: one repeated token, the rest new.
    raw = s.raw(source)
    write_synth(raw / "part-0002.txt", 200, seed=99, start=10_000)
    with (raw / "part-0002.txt").open("ab") as f:
        f.write(f"{before['token'][0]} {' '.join(['0.1'] * 50)}\n".encode())
    src = read_json(raw / "SOURCE.json")
    src["files"].append(
        {"name": "part-0002.txt", "bytes": (raw / "part-0002.txt").stat().st_size, "sha256": "late"}
    )
    write_json(raw / "SOURCE.json", src)

    second = ingest.run(s, source)
    after = pq.read_table(s.work(source) / "meta.parquet").to_pydict()
    assert second["accepted"] > first["accepted"]
    assert after["token"][: first["accepted"]] == before["token"], "earlier rows keep their ids"
    late = [
        r for r in pq.read_table(s.work(source) / "quarantine.parquet").to_pylist() if r["source_file"] == 1
    ]
    assert any(
        r["reason"] == "duplicate_token" and r["head"].startswith(before["token"][0] + " ") for r in late
    ), "the repeat of an earlier token is quarantined, first occurrence wins"


def test_an_input_with_no_valid_rows_stops_with_a_message(tmp_path):
    s = make_settings(tmp_path)
    raw = s.raw("synth-4000")
    raw.mkdir(parents=True)
    (raw / "part-0001.txt").write_bytes(b"x 1 2\n\xff\n")
    (raw / "SOURCE.json").write_text(
        '{"dim": 50, "url": null, "files": [{"name": "part-0001.txt", "bytes": 9, "sha256": "x"}]}',
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="no valid rows"):
        ingest.run(dataclasses.replace(s), "synth-4000")
