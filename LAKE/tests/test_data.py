"""Data tests: a pipeline that runs green and produces wrong data must fail here."""

from __future__ import annotations

import http.server
import threading
from pathlib import Path

import duckdb
import pytest

from bakeoff import bench, data
from bakeoff.cli import main
from bakeoff.config import ConfigError, Settings, read_json, write_json
from tests.conftest import make_settings

MONTHS = ("2024-01", "2024-02")


def clean(pipeline) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.read_parquet(str(pipeline.s.data_dir / "clean" / "source.parquet")).create_view("t")
    return con


def one(con: duckdb.DuckDBPyConnection, sql: str):
    return con.execute(sql).fetchone()[0]


# ---------------------------------------------------------------- generator

def test_generator_is_deterministic(tmp_path: Path) -> None:
    a, b, c = tmp_path / "a.csv", tmp_path / "b.csv", tmp_path / "c.csv"
    assert data.synth_taxi(a, 2000, 8, MONTHS) == data.synth_taxi(b, 2000, 8, MONTHS)
    data.synth_taxi(c, 2000, 9, MONTHS)
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() != c.read_bytes()


# ---------------------------------------------------------------- ingest

def test_every_landed_line_is_accounted_for(pipeline) -> None:
    ing = pipeline.ingest
    assert ing["landed"] == data.count_lines(pipeline.landing) - 1
    assert ing["landed"] == ing["clean"] + sum(ing["quarantined"].values())
    assert ing["clean"] == one(clean(pipeline), "SELECT count(*) FROM t")


def test_each_injected_defect_is_quarantined_under_its_own_reason(pipeline) -> None:
    quarantined, injected = pipeline.ingest["quarantined"], pipeline.injected
    for reason in ("pickup_far_outside_file_month", "dropoff_before_pickup", "trip_distance_implausible",
                   "duplicate", "unparseable"):
        assert quarantined[reason] == injected[reason], reason
    assert set(quarantined) <= {*injected, "null_in_required", "location_out_of_range"}


def test_quarantine_keeps_the_evidence(pipeline) -> None:
    con = duckdb.connect()
    con.read_parquet(str(pipeline.s.data_dir / "quarantine" / "rejects.parquet")).create_view("q")
    bad_lines = con.execute("SELECT line, raw FROM q WHERE reason = 'unparseable' ORDER BY line").fetchall()
    assert len(bad_lines) == 5 and all(line > pipeline.s.synth_rows for line, _ in bad_lines)
    raws = " ".join(raw for _, raw in bad_lines)
    assert "far" in raws and "08:30:00 AM" in raws and "surplus" in raws  # the original text, not a summary
    assert one(con, "SELECT count(*) FROM q WHERE reason = 'trip_distance_implausible' "
                    "AND json_extract(raw, '$.trip_distance')::DOUBLE > 500") == 4


def test_real_but_odd_rows_are_kept_and_counted(pipeline) -> None:
    warnings, con = pipeline.ingest["warnings"], clean(pipeline)
    assert warnings["late_arrival"] == pipeline.injected["late_arrival"]
    assert 0 < warnings["negative_amount"] <= pipeline.injected["negative_amount"]
    assert warnings["negative_amount"] == one(con, "SELECT count(*) FROM t WHERE fare_amount < 0 OR total_amount < 0")


def test_clean_data_invariants(pipeline) -> None:
    con = clean(pipeline)
    rows = one(con, "SELECT count(*) FROM t")
    assert rows == one(con, "SELECT count(*) FROM (SELECT DISTINCT * FROM t)")  # uniqueness
    assert one(con, "SELECT count(*) FROM (SELECT pickup_datetime < lag(pickup_datetime) OVER () AS back FROM t) "
                    "WHERE back") == 0  # sorted on the sort key
    assert one(con, "SELECT count(*) FROM t WHERE dropoff_datetime < pickup_datetime OR trip_distance > 500 "
                    "OR trip_distance < 0 OR pickup_datetime IS NULL OR vendor_id IS NULL") == 0
    # referential integrity: every zone id is in the TLC zone table, every row's lineage is a configured month
    assert one(con, "SELECT count(*) FROM t WHERE pu_location_id NOT BETWEEN 1 AND 265 "
                    "OR do_location_id NOT BETWEEN 1 AND 265") == 0
    assert {r[0] for r in con.execute("SELECT DISTINCT source_month FROM t").fetchall()} == set(pipeline.s.months)
    # distribution: the ~4.7% metadata-less rows survive as NULLs, and they are exactly the payment_type 0 rows
    null_rate = one(con, "SELECT avg((passenger_count IS NULL)::INT) FROM t")
    assert 0.03 < null_rate < 0.07
    assert one(con, "SELECT count(*) FROM t WHERE (passenger_count IS NULL) <> (payment_type = 0)") == 0


def test_ingest_is_idempotent(pipeline) -> None:
    s, schema = pipeline.s, pipeline.ingest["schema"]
    before = bench.checksum(clean(pipeline), schema)
    info: dict = {}
    data.ingest(s, pipeline.landing, pipeline.ds, info)
    assert info["skipped"] is True
    stamp = read_json(s.data_dir / "clean" / "ingest.json")
    write_json(s.data_dir / "clean" / "ingest.json", stamp | {"fingerprint": "stale"})  # force a real re-run
    data.ingest(s, pipeline.landing, pipeline.ds, {})
    assert bench.checksum(clean(pipeline), schema) == before
    assert read_json(s.data_dir / "clean" / "ingest.json") == stamp


def test_csv_that_breaks_the_contract_is_refused(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    landing, ds = data.taxi_dataset(s)
    landing.parent.mkdir(parents=True)
    landing.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(data.DataError, match="does not match the dataset contract"):
        data.ingest(s, landing, ds, {})


# ---------------------------------------------------------------- fetch: failure injection

class Flaky(http.server.BaseHTTPRequestHandler):
    """Fails `fail_first` requests with `status`, then serves `body`."""

    fail_first, status, body, seen = 0, 503, b"payload", 0

    def do_GET(self) -> None:  # noqa: N802
        cls = type(self)
        cls.seen += 1
        if cls.seen <= cls.fail_first:
            self.send_error(cls.status)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(cls.body)))
        self.end_headers()
        self.wfile.write(cls.body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def server():
    def start(fail_first: int, status: int = 503) -> str:
        Flaky.fail_first, Flaky.status, Flaky.seen = fail_first, status, 0
        return f"http://127.0.0.1:{httpd.server_port}"
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield start
    httpd.shutdown()


def test_download_retries_transient_failures(server, tmp_path: Path) -> None:
    data.download(server(fail_first=2) + "/f", tmp_path / "f", backoff=0.01)
    assert (tmp_path / "f").read_bytes() == b"payload" and Flaky.seen == 3


def test_download_gives_up_with_an_actionable_error(server, tmp_path: Path) -> None:
    with pytest.raises(data.DataError, match="LAKE_SOURCE=synthetic"):
        data.download(server(fail_first=99) + "/f", tmp_path / "f", attempts=3, backoff=0.01)
    assert Flaky.seen == 3 and list(tmp_path.iterdir()) == []  # capped, and no half-written file left behind


def test_download_does_not_retry_a_missing_file(server, tmp_path: Path) -> None:
    with pytest.raises(data.DataError, match="HTTP 404"):
        data.download(server(fail_first=99, status=404) + "/f", tmp_path / "f", backoff=0.01)
    assert Flaky.seen == 1


def test_raw_files_are_immutable(server, tmp_path: Path) -> None:
    s = make_settings(tmp_path, LAKE_SOURCE="tlc", LAKE_MONTHS="2024-01", LAKE_TLC_BASE_URL=server(fail_first=0))
    data.fetch(s, {})
    raw = s.data_dir / "raw" / "yellow_tripdata_2024-01.parquet"
    data.fetch(s, {})  # idempotent: nothing downloaded twice
    assert Flaky.seen == 1
    raw.chmod(0o666)
    raw.write_bytes(b"tampered")
    with pytest.raises(data.DataError, match="immutable"):
        data.fetch(s, {})


# ---------------------------------------------------------------- your own data

def test_bake_off_runs_on_an_arbitrary_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv = tmp_path / "events.csv"
    csv.write_text("event_id,kind,amount\n" + "".join(f"{i},{'ab'[i % 2]},{i * 1.5}\n" for i in range(3000)),
                   encoding="utf-8")
    s = make_settings(tmp_path)
    for key, value in {"LAKE_DATA_DIR": str(s.data_dir), "LAKE_PUBLISHED_DIR": str(s.published_dir),
                       "LAKE_COLD_RUNS": "1", "LAKE_WARM_RUNS": "2", "LAKE_LAB_ROWS": "1000",
                       "LAKE_CELL_BUDGET_S": "0", "LAKE_LOG_LEVEL": "ERROR"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)  # no stray .env
    assert main(["all", "--csv", str(csv), "--sort-key", "event_id", "--only", "csv-none|parquet-zstd3$|orc-zstd$"]) == 0
    results = read_json(s.data_dir / "results" / "results.json")
    assert results["dataset"]["rows"] == 3000 and not results["partial"]
    assert [v["id"] for v in results["variants"]] == ["csv-none", "parquet-zstd3", "orc-zstd"]
    assert csv.read_text(encoding="utf-8").startswith("event_id,kind,amount\n0,a,0.0")  # the input is never touched


def test_unknown_sort_key_is_explained(tmp_path: Path) -> None:
    csv = tmp_path / "x.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(data.DataError, match="Columns: a, b"):
        data.generic_dataset(csv, "nope")


# ---------------------------------------------------------------- configuration boundary

@pytest.mark.parametrize("env, needle", [
    ({"LAKE_MONTHS": "2024-13"}, "LAKE_MONTHS"), ({"LAKE_SOURCE": "s3"}, "LAKE_SOURCE"),
    ({"LAKE_WARM_RUNS": "1"}, "between 2 and"), ({"LAKE_COLD_RUNS": "many"}, "not an integer"),
    ({"LAKE_TLC_BASE_URL": "file:///etc"}, "http"), ({"LAKE_CELL_BUDGET_S": "soon"}, "number of seconds"),
])
def test_bad_configuration_names_the_variable(env: dict[str, str], needle: str) -> None:
    with pytest.raises(ConfigError, match=needle):
        Settings.from_env(env)
