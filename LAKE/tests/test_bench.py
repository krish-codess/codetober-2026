"""Formats, the measurement method, and the cost model."""

from __future__ import annotations

import time
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.orc as orc
import pyarrow.parquet as pq
import pytest

from bakeoff import bench, data
from bakeoff.config import read_json
from bakeoff.formats import (
    LAB_MATRIX,
    MATRIX,
    CountingFS,
    Variant,
    anatomy,
    evict,
    eviction_check,
    shuffled,
    write_variant,
)
from bakeoff.report import Workload, WorkloadError, monthly_cost, pricing, pushdown, recommend, report
from tests.conftest import make_settings

SCHEMA = [["id", "BIGINT"], ["grp", "BIGINT"], ["amount", "DOUBLE"], ["label", "VARCHAR"], ["noise", "BIGINT"]]
FULL = "SELECT max(COLUMNS(*)) FROM t"
PROJECTION = "SELECT min(id), sum(amount) FROM t"
CLUSTERED = PROJECTION + " WHERE id >= 100000 AND id < 102000"


@pytest.fixture(scope="module")
def table() -> pa.Table:
    """200k rows sorted on `id`, with a wide incompressible column so projection has something to skip."""
    rng = np.random.default_rng(1)
    n = 200_000
    return pa.table({"id": np.arange(n), "grp": rng.integers(0, 50, n), "amount": np.round(rng.random(n) * 100, 2),
                     "label": np.array(["card", "cash", "void"])[rng.integers(0, 3, n)],
                     "noise": rng.integers(0, 2**62, n)})


def bytes_read(tmp: Path, table: pa.Table, v: Variant, sql: str) -> tuple[int, int]:
    """(bytes pulled from storage by `sql`, file size) for `table` stored as variant `v`."""
    path = tmp / v.id / v.filename
    if not path.exists():
        write_variant(shuffled(table, 8) if v.layout == "shuffled" else table, v, path)
    fs = CountingFS()
    bench.execute(duckdb.connect(), v, path, SCHEMA, sql, {}, fs)
    return fs.bytes, path.stat().st_size


# ---------------------------------------------------------------- identical data in every variant

def test_every_variant_was_materialised_and_verified(pipeline) -> None:
    manifest = read_json(pipeline.s.data_dir / "variants" / "manifest.json")
    assert set(manifest) == {v.id for v in MATRIX} and len(MATRIX) == 24
    assert {m["rows"] for m in manifest.values()} == {pipeline.ingest["clean"]}
    assert {v.format for v in MATRIX} == {"csv", "parquet", "orc", "avro"}


@pytest.mark.parametrize("v", [Variant("parquet", "zstd", 3), Variant("orc", "zstd"), Variant("avro", "snappy"),
                               Variant("csv", "gzip")], ids=lambda v: v.id)
def test_checksum_survives_a_round_trip_and_catches_one_changed_cell(v: Variant, table: pa.Table, tmp_path: Path) -> None:
    def fingerprint(t: pa.Table) -> tuple[int, int]:
        path = tmp_path / f"{t.num_rows}-{abs(hash(t['amount'][0].as_py()))}" / v.filename
        write_variant(t, v, path)
        con = duckdb.connect()
        bench.open_source(con, v, path, SCHEMA)
        return bench.checksum(con, SCHEMA)

    small = table.slice(0, 5000)
    con = duckdb.connect()
    con.register("t", small)
    expected = bench.checksum(con, SCHEMA)
    assert fingerprint(small) == expected
    assert fingerprint(shuffled(small, 3)) == expected  # order-independent by design
    amounts = small["amount"].to_pylist()
    amounts[0] += 0.01
    assert fingerprint(small.set_column(2, "amount", pa.array(amounts))) != expected


def test_materialize_refuses_a_variant_that_changed_the_data(pipeline, tmp_path, monkeypatch) -> None:
    s = make_settings(tmp_path)
    data.land(s, {})
    data.ingest(s, *data.taxi_dataset(s), {})
    monkeypatch.setattr(bench, "write_variant", lambda t, v, p: write_variant(t.slice(1), v, p))  # drops a row
    with pytest.raises(data.DataError, match="does not hold the source data"):
        bench.materialize(s, {}, "parquet-snappy")
    assert not bench.variant_path(s, Variant("parquet", "snappy")).exists()


def test_chunk_size_settings_reach_the_files(table: pa.Table, tmp_path: Path) -> None:
    for v, expected in [(Variant("parquet", "zstd", 3, 10_000), 20), (Variant("parquet", "zstd", 3), 1)]:
        write_variant(table, v, tmp_path / v.id)
        assert pq.ParquetFile(tmp_path / v.id).metadata.num_row_groups == expected == anatomy(v, tmp_path / v.id)["chunks"]
    small, big = Variant("orc", "none", chunk=1 << 20), Variant("orc", "none")
    for v in (small, big):
        write_variant(table, v, tmp_path / v.id)
    assert orc.ORCFile(str(tmp_path / small.id)).nstripes > orc.ORCFile(str(tmp_path / big.id)).nstripes == 1


def test_orc_lz4_only_compresses_when_asked_to(table: pa.Table, tmp_path: Path) -> None:
    sizes = {}
    for v in (Variant("orc", "none"), Variant("orc", "lz4"), Variant("orc", "lz4", 1)):
        write_variant(table.select(["label", "grp"]), v, tmp_path / v.id)
        sizes[v.id] = (tmp_path / v.id).stat().st_size
    assert sizes["orc-lz4-best"] < 0.8 * sizes["orc-lz4"]  # the default "speed" strategy leaves most blocks raw


# ---------------------------------------------------------------- bytes read proves pushdown

def test_projection_pushdown_reads_only_the_columns_asked_for(table: pa.Table, tmp_path: Path) -> None:
    for v in (Variant("parquet", "zstd", 3), Variant("orc", "zstd")):
        full, size = bytes_read(tmp_path, table, v, FULL)
        projected, _ = bytes_read(tmp_path, table, v, PROJECTION)
        assert full >= 0.95 * size, v.id          # the counter sees a full scan as the whole file
        assert projected < 0.5 * full, v.id       # two of five columns, and not the wide one


def test_predicate_pushdown_skips_row_groups_only_when_data_is_clustered(table: pa.Table, tmp_path: Path) -> None:
    v = Variant("parquet", "zstd", 3, 10_000)
    control, _ = bytes_read(tmp_path, table, v, PROJECTION)
    filtered, _ = bytes_read(tmp_path, table, v, CLUSTERED)
    assert filtered < 0.25 * control  # 1% of rows, 1 of 20 row groups; the rest is footer
    lost = Variant("parquet", "zstd", 3, 10_000, layout="shuffled")
    control, _ = bytes_read(tmp_path, table, lost, PROJECTION)
    filtered, _ = bytes_read(tmp_path, table, lost, CLUSTERED)
    assert filtered > 0.9 * control   # same query, same format, same filter: sort order gone, nothing to skip


def test_one_big_row_group_cannot_be_pruned(table: pa.Table, tmp_path: Path) -> None:
    v = Variant("parquet", "zstd", 3)
    control, _ = bytes_read(tmp_path, table, v, PROJECTION)
    filtered, _ = bytes_read(tmp_path, table, v, CLUSTERED)
    assert filtered > 0.9 * control


def test_row_formats_and_this_orc_reader_have_no_predicate_pushdown(table: pa.Table, tmp_path: Path) -> None:
    for v in (Variant("csv", "none"), Variant("avro", "snappy")):
        filtered, size = bytes_read(tmp_path, table, v, CLUSTERED)
        assert filtered >= size, v.id
    # Pinned behaviour of PyArrow's ORC dataset reader: columns are projected, stripes are never skipped.
    orc_small = Variant("orc", "zstd", chunk=1 << 20)
    control, _ = bytes_read(tmp_path, table, orc_small, PROJECTION)
    filtered, _ = bytes_read(tmp_path, table, orc_small, CLUSTERED)
    assert filtered > 0.9 * control


def test_repeat_queries_are_not_served_from_an_engine_cache(table: pa.Table, tmp_path: Path) -> None:
    v = Variant("parquet", "zstd", 3)
    path = tmp_path / v.filename
    write_variant(table, v, path)
    fs, con = CountingFS(), duckdb.connect()
    bench.execute(con, v, path, SCHEMA, PROJECTION, {}, fs)
    first = fs.bytes
    bench.execute(con, v, path, SCHEMA, PROJECTION, {}, fs)
    assert first > 0 and fs.bytes == 2 * first


def test_eviction_is_reported_not_assumed(table: pa.Table, tmp_path: Path) -> None:
    path = tmp_path / "f.parquet"
    write_variant(table, Variant("parquet", "none"), path)
    check = eviction_check(path)
    assert isinstance(evict(path), bool)
    assert check["cached_mb_s"] > 0 and check["evicted_mb_s"] > 0
    assert check["effective"] in (True, False) and (check["supported"] or not check["effective"])


# ---------------------------------------------------------------- runner

def test_results_are_complete_and_correct(pipeline) -> None:
    r = pipeline.results
    assert pipeline.failed == 0 and r["partial"] is False and r["missing"] == []
    assert len(r["variants"]) == len(MATRIX)
    for v in r["variants"]:
        assert set(v["queries"]) == {q["id"] for q in pipeline.ds["queries"]}
        for qid, cell in v["queries"].items():
            assert cell["ok"] is True, (v["id"], qid)   # same answer as clean/source.parquet
            assert cell["cold"]["n"] >= 1 and cell["warm"]["n"] >= 2
            assert cell["warm"]["min_s"] <= cell["warm"]["median_s"] <= cell["warm"]["max_s"]
            assert cell["warm"]["cv"] >= 0 and cell["bytes_read"] > 0 and cell["reads"] > 0
    assert r["env"]["settings"]["cold_runs"] == 1 and "effective" in r["env"]["eviction"]
    assert {s for s in r["stages"]} >= {"land", "ingest"} or r["stages"] == {}


def test_bench_resumes_instead_of_repeating(pipeline) -> None:
    info: dict = {}
    assert bench.bench(pipeline.s, pipeline.ds, info) == 0
    assert info["rows"] == 0


def test_a_wrong_answer_is_recorded_as_a_failure(table: pa.Table, tmp_path: Path) -> None:
    v = Variant("parquet", "zstd", 3)
    path = tmp_path / v.filename
    write_variant(table, v, path)
    records = []
    bench.run_cell(make_settings(tmp_path), v, path, SCHEMA, "SELECT count(*) FROM t", {}, [(199_999,)],
                   lambda kind, **f: records.append((kind, f["ok"])))
    assert [k for k, _ in records] == ["cold", "counted", "warm", "warm"] and not any(ok for _, ok in records)


def test_same_tolerates_float_noise_and_nothing_else() -> None:
    assert bench.same([(1, 0.1 + 0.2, "a")], [(1, 0.3, "a")])
    assert not bench.same([(1, 0.3001)], [(1, 0.3)])
    assert not bench.same([(1,)], [(1,), (2,)])
    assert not bench.same([("a",)], [("b",)])


def test_repeat_respects_minimum_maximum_and_budget() -> None:
    calls: list[int] = []
    bench.repeat(lambda: calls.append(1), most=5, least=2, budget_s=0)
    assert len(calls) == 2      # budget spent: stop at the floor
    bench.repeat(lambda: calls.append(1), most=5, least=2, budget_s=60)
    assert len(calls) == 7      # budget to spare: run all five
    slow: list[int] = []
    bench.repeat(lambda: (time.sleep(0.03), slow.append(1)), most=50, least=1, budget_s=0.05)
    assert 2 <= len(slow) <= 4


def test_corrupt_variant_fails_alone_and_is_retried(tmp_path: Path) -> None:
    """Failure injection: truncate one file mid-run. The others still report; a re-run heals only the gap."""
    s = make_settings(tmp_path)
    landing, ds = data.taxi_dataset(s)
    data.land(s, {})
    data.ingest(s, landing, ds, {})
    only = "parquet-(snappy|lz4)$"
    bench.materialize(s, {}, only)
    victim = bench.variant_path(s, Variant("parquet", "snappy"))
    good = victim.read_bytes()
    victim.write_bytes(good[: len(good) // 2])

    assert bench.bench(s, ds, {}, only) == len(ds["queries"])
    report(s, ds, {})
    r = read_json(s.data_dir / "results" / "results.json")
    assert r["partial"] is True and len(r["missing"]) == len(ds["queries"])
    assert all(m.startswith("parquet-snappy/") for m in r["missing"])
    broken, healthy = r["variants"]
    assert "error" in broken["queries"]["count"] and healthy["queries"]["count"]["ok"]
    rec = recommend(r, Workload())
    assert rec["pick"] == "parquet-lz4" and rec["excluded"][0]["variant"] == "parquet-snappy"

    victim.write_bytes(good)
    info: dict = {}
    assert bench.bench(s, ds, info, only) == 0
    assert info["rows"] == len(ds["queries"]) * 4  # only the failed cells ran: 1 cold + 1 counted + 2 warm
    report(s, ds, {})
    assert read_json(s.data_dir / "results" / "results.json")["partial"] is False


# ---------------------------------------------------------------- analytics

def test_pushdown_analysis_matches_the_cells(pipeline) -> None:
    by_id = {p["variant"]: p for p in pipeline.results["pushdown"]}
    assert by_id["parquet-zstd3"]["projection_pushdown"] is True
    assert by_id["orc-zstd"]["projection_pushdown"] is True
    for row_format in ("csv-none", "avro-snappy"):
        assert by_id[row_format]["projection_pushdown"] is False
        assert by_id[row_format]["predicate_pushdown"] is False
    v = next(v for v in pipeline.results["variants"] if v["id"] == "parquet-zstd3")
    assert by_id["parquet-zstd3"]["read_fraction"]["projection"] == pytest.approx(
        v["queries"]["projection"]["bytes_read"] / v["bytes"])


def test_pushdown_verdicts() -> None:
    def variant(projection: int, clustered: int) -> dict:
        return {"id": "x", "format": "parquet", "reader": "r", "bytes": 1000,
                "queries": {"projection": {"bytes_read": projection}, "filter_clustered": {"bytes_read": clustered}}}
    queries = [{"id": "projection"}, {"id": "filter_clustered", "control": "projection"}]
    pruned = pushdown([variant(200, 20)], queries)[0]
    assert pruned["projection_pushdown"] and pruned["predicate_pushdown"]
    assert pruned["pruned_vs_control"]["filter_clustered"] == pytest.approx(0.9)
    blind = pushdown([variant(1000, 1000)], queries)[0]
    footer_heavy = variant(960, 96)  # a tiny file: the footer is most of every read
    footer_heavy["queries"]["full_scan"] = {"bytes_read": 4000}
    assert pushdown([footer_heavy], [{"id": "full_scan"}, *queries])[0]["projection_pushdown"]
    assert not blind["projection_pushdown"] and not blind["predicate_pushdown"]


def test_column_lab_shows_what_compression_depends_on(pipeline) -> None:
    lab = pipeline.results["columns"]["lab"]
    assert len(lab["cells"]) == len(lab["columns"]) * len(LAB_MATRIX)
    ratio = {(c["column"], c["variant"]): c["ratio"] for c in lab["cells"]}
    for v in LAB_MATRIX:
        assert ratio[("int_random", v.id)] < 1.3, v.id                   # entropy does not compress, in any format
    assert ratio[("int_low_card_sorted", "parquet-zstd3")] > 2 * ratio[("int_low_card", "parquet-zstd3")]  # order
    assert ratio[("int_low_card", "parquet-zstd3")] > 3 * ratio[("int_mid_card", "parquet-zstd3")]        # cardinality
    assert ratio[("int_mid_card_null90", "parquet-zstd3")] > 2 * ratio[("int_mid_card_null10", "parquet-zstd3")]  # nulls
    real = pipeline.results["columns"]["real"]
    assert {c["type"] for c in real} >= {"INTEGER", "DOUBLE", "TIMESTAMP", "VARCHAR"}
    assert all(c["compressed"] > 0 for c in real)


# ---------------------------------------------------------------- cost model

VARIANT = {"id": "v", "format": "parquet", "codec": "zstd", "layout": "sorted", "bytes": 1_000_000_000,
           "write_cpu_s": 36.0,
           "queries": {"scan": {"bytes_read": 50_000_000, "reads": 10, "cpu_s": 0.36, "warm": {"median_s": 0.2}, "ok": True},
                       "tiny": {"bytes_read": 1_000, "reads": 1, "cpu_s": 0.036, "warm": {"median_s": 0.01}, "ok": True}}}
CLASSES = {"scan": ["scan"], "tiny": ["tiny"]}
CSV_BYTES = 4_000_000_000
AWS = pricing()["providers"]["aws"]


def test_cost_model_by_hand_serverless() -> None:
    w = Workload(dataset_gb=400, queries_per_month=1000, rewrites_per_month=2, provider="aws", engine="serverless")
    cost = monthly_cost(VARIANT, CLASSES, w, {"scan": 1.0}, AWS, CSV_BYTES)  # scale = 400 GB / 4 GB = 100
    assert cost["storage"] == pytest.approx(100 * 0.023)                  # 1 GB x 100 stored
    assert cost["scan"] == pytest.approx(1000 * 5e9 / 1e12 * 5.00)        # 50 MB x 100 per query
    assert cost["requests"] == pytest.approx(1000 * 1000 / 1000 * 0.0004)  # 10 reads x 100 per query
    assert cost["write"] == pytest.approx(2 * 1.0 * 0.0446)               # 36 s x 100 = 1 CPU-hour, twice
    assert cost["compute"] == 0
    assert cost["total"] == pytest.approx(2.30 + 25.0 + 0.40 + 0.0892)
    assert cost["latency_s"] == pytest.approx(0.2)


def test_cost_model_applies_the_per_query_minimum() -> None:
    w = Workload(dataset_gb=400, queries_per_month=1000, rewrites_per_month=0)
    cost = monthly_cost(VARIANT, CLASSES, w, {"tiny": 1.0}, AWS, CSV_BYTES)
    assert cost["scan"] == pytest.approx(1000 * 10e6 / 1e12 * 5.00)  # 100 KB scanned, 10 MB billed


def test_cost_model_by_hand_self_hosted() -> None:
    w = Workload(dataset_gb=400, queries_per_month=1000, rewrites_per_month=0, engine="self_hosted")
    cost = monthly_cost(VARIANT, CLASSES, w, {"scan": 0.5, "tiny": 0.5}, AWS, CSV_BYTES)
    assert cost["scan"] == 0
    assert cost["compute"] == pytest.approx((500 * 36 + 500 * 3.6) / 3600 * 0.0446)
    assert cost["latency_s"] == pytest.approx(0.5 * 0.2 + 0.5 * 0.01)


def test_recommendation_follows_the_objective(pipeline) -> None:
    r = pipeline.results
    by_cost = recommend(r, Workload(objective="cost"))
    by_speed = recommend(r, Workload(objective="speed"))
    assert by_cost["pick"] == min(by_cost["ranked"], key=lambda x: x["total"])["variant"]
    assert by_speed["pick"] == min(by_speed["ranked"], key=lambda x: x["latency_s"])["variant"]
    for row in by_cost["ranked"]:
        assert row["total"] == pytest.approx(sum(row[k] for k in ("storage", "scan", "compute", "requests", "write")))
    assert [x["variant"] for x in by_cost["excluded"]] == ["parquet-zstd3-shuffled"]  # a control, never a pick
    assert by_cost["pick"] in by_cost["why"][0] and "$" in by_cost["why"][0]


def test_recommendation_mix_is_normalised_and_validated(pipeline) -> None:
    r = pipeline.results
    a = recommend(r, Workload(mix={"full_scan": 1, "aggregate": 3}))
    b = recommend(r, Workload(mix={"full_scan": 25, "aggregate": 75, "metadata": 0}))
    assert a["workload"]["mix"] == b["workload"]["mix"] == {"full_scan": 0.25, "aggregate": 0.75}
    assert a["ranked"] == b["ranked"]
    with pytest.raises(WorkloadError, match="Unknown query class"):
        recommend(r, Workload(mix={"joins": 1}))
    with pytest.raises(WorkloadError, match="at least one"):
        recommend(r, Workload(mix={"full_scan": 0}))


def test_storage_heavy_and_scan_heavy_workloads_disagree_about_csv(pipeline) -> None:
    """The model must be able to tell workloads apart, or it is not a model."""
    r = pipeline.results
    cold_archive = recommend(r, Workload(dataset_gb=1000, queries_per_month=0, objective="cost"))
    assert all(x["scan"] == 0 for x in cold_archive["ranked"])
    assert cold_archive["pick"] == min(cold_archive["ranked"], key=lambda x: x["storage"] + x["write"])["variant"]
    busy = recommend(r, Workload(dataset_gb=1000, queries_per_month=1_000_000, mix={"projection": 1}, objective="cost"))
    csv = next(x for x in busy["ranked"] if x["variant"] == "csv-none")
    quiet = next(x for x in cold_archive["ranked"] if x["variant"] == "csv-none")
    assert csv["scan"] > 100 * csv["storage"] and csv["storage"] == pytest.approx(quiet["storage"])
    assert csv["total"] > busy["ranked"][0]["total"]  # the ranking at real scale is asserted by the e2e test
