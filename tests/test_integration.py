"""Integration + data tests against a real PostgreSQL (fresh database per session, real migrations).

A pipeline that runs and produces wrong data must fail here: these assert invariants of the
published data, its reproducibility from raw, and its behaviour under late data.
"""

from __future__ import annotations

import math
import shutil
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import polars as pl
import psycopg
import pytest

from goldstandard import db, migrate
from goldstandard.index import WEIGHT_CAP
from goldstandard.pipeline import Pipeline
from goldstandard.raw import RawStore
from goldstandard.sources import synthetic
from tests.conftest import ADMIN_DSN, REFERENCE, _swap_db, make_settings, small_config

pytestmark = pytest.mark.integration


@contextmanager
def fresh_db(pg):
    name = f"gs_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}" OWNER gs_owner')
    dsns = {k: _swap_db(pg[k], name) for k in ("owner", "pipeline", "api")}
    migrate.upgrade(dsns["owner"])
    try:
        yield dsns
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def q(dsn: str, sql: str, params=()):
    with psycopg.connect(dsn) as c:
        return c.execute(sql, params).fetchall()


# ------------------------------------------------------------------------------------------ schema + security
def test_migrations_roll_back_and_forward_cleanly(pg):
    with fresh_db(pg) as d:
        latest = len(migrate.discover())
        assert migrate.downgrade(d["owner"], 0) == list(range(latest, 0, -1))
        assert migrate.upgrade(d["owner"]) == list(range(1, latest + 1))
        with psycopg.connect(d["owner"]) as c:
            c.execute("UPDATE schema_migrations SET checksum = repeat('0', 64) WHERE version = 1")
        with pytest.raises(RuntimeError, match="edited after being applied"):
            migrate.upgrade(d["owner"])


def test_published_tables_are_append_only_even_for_the_owner(seeded):
    for table in ("index_value", "item_price_daily", "basket_item", "basket_period", "basket_link"):
        with (
            pytest.raises(psycopg.errors.RestrictViolation, match="append-only"),
            psycopg.connect(seeded["owner"]) as c,
        ):
            c.execute(
                f"UPDATE {table} SET world_id = world_id"
                if table.startswith("basket")
                else f"DELETE FROM {table} WHERE true"
            )


def test_roles_have_least_privilege(seeded):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(seeded["api"]) as c:
        c.execute("INSERT INTO division (division_id, label) VALUES ('x', 'x')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(seeded["pipeline"]) as c:
        c.execute("SELECT * FROM api_key")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(seeded["pipeline"]) as c:
        c.execute("UPDATE index_value SET value = 1")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(seeded["api"]) as c:
        c.execute("CREATE TABLE evil (x int)")


# ------------------------------------------------------------------------------------------ data invariants
def test_every_expected_cell_has_exactly_one_current_price_per_processed_day(seeded):
    for world in ("synthetic", "eve"):
        cells = seeded["pipe"].expected_cells(world).height
        rows = q(
            seeded["pipeline"],
            """
            SELECT p.day, count(*), count(DISTINCT (p.server_id, p.item_id)) FROM item_price_current p
            JOIN server s USING (server_id) WHERE s.world_id = %s GROUP BY p.day""",
            (world,),
        )
        assert rows and all(n == cells and d == cells for _, n, d in rows), world


def test_no_price_is_ever_fabricated(seeded):
    bad = q(
        seeded["pipeline"],
        """SELECT count(*) FROM item_price_current
                                   WHERE (status = 'ok') <> (price IS NOT NULL) OR (status = 'ok' AND n_obs = 0)""",
    )
    assert bad[0][0] == 0
    statuses = dict(q(seeded["pipeline"], "SELECT status, count(*) FROM item_price_current GROUP BY 1"))
    assert statuses.get("thin", 0) > 0 or statuses.get("missing", 0) > 0  # thin markets exist and are reported


def test_basket_weights_sum_to_one_and_respect_the_cap(seeded):
    for world, period, total in q(
        seeded["pipeline"], "SELECT world_id, period_id, sum(weight) FROM basket_item GROUP BY 1, 2"
    ):
        assert math.isclose(total, 1.0, rel_tol=1e-9), (world, period)
    shares = q(
        seeded["pipeline"],
        """SELECT world_id, period_id, server_id, max(weight) / sum(weight), count(*)
                                      FROM basket_item GROUP BY 1, 2, 3""",
    )
    for *_, share, n in shares:
        assert share <= WEIGHT_CAP + 1e-9 or n * WEIGHT_CAP <= 1


def test_index_values_are_sane_and_status_matches_coverage(seeded):
    rows = q(seeded["pipeline"], "SELECT value, coverage, status FROM index_value_current")
    assert rows
    for value, coverage, status in rows:
        assert (value is None) == (status == "insufficient")
        if value is not None:
            assert 10 < value < 1000
        if status == "ok":
            assert coverage >= 0.9


def test_first_basket_day_starts_at_reference_100(seeded):
    rows = q(
        seeded["pipeline"],
        """
        SELECT v.value FROM index_value_current v JOIN index_series s USING (series_id)
        JOIN basket_period b ON b.world_id = s.world_id AND b.period_id = v.period_id
        WHERE s.server_id IS NULL AND s.division_id IS NULL AND v.day = b.valid_from
        ORDER BY b.valid_from LIMIT 1""",
    )
    assert rows and abs(rows[0][0] - 100) < 15  # 100 at the link point; the first day may already have moved


def test_synthetic_index_tracks_the_true_fair_price_index(seeded, small_world):
    """Differential test: the index computed from noisy, defective, manipulated snapshots vs the same basket
    evaluated on the simulator's TRUE fair prices. Errors here mean the estimators are not doing their job."""
    truth = pl.read_parquet(small_world["root"] / "truth" / "fair.parquet")
    basket = pl.DataFrame(
        q(
            seeded["pipeline"],
            """SELECT server_id, item_id, weight FROM basket_item
                                                  WHERE world_id = 'synthetic' AND period_id = '2025Q4'""",
        ),
        schema=["server_id", "item_id", "weight"],
        orient="row",
    )
    p = q(
        seeded["pipeline"],
        "SELECT link_from, link_to FROM basket_period WHERE world_id='synthetic' AND period_id='2025Q4'",
    )
    link_from, link_to = p[0]
    t = truth.join(basket, on=["server_id", "item_id"])
    base = (
        t.filter(pl.col("day").is_between(link_from, link_to))
        .group_by("server_id", "item_id")
        .agg(p0=pl.col("fair").median())
    )
    true_idx = (
        t.join(base, on=["server_id", "item_id"])
        .filter(pl.col("day") >= date(2025, 10, 1))
        .group_by("day")
        .agg(true=(pl.col("weight") * pl.col("fair") / pl.col("p0")).sum() / pl.col("weight").sum() * 100)
    )
    pub = pl.DataFrame(
        q(
            seeded["pipeline"],
            """SELECT v.day, v.value FROM index_value_current v JOIN index_series s USING (series_id)
                                               WHERE s.world_id='synthetic' AND s.server_id IS NULL AND s.division_id IS NULL""",
        ),
        schema=["day", "value"],
        orient="row",
    )
    j = true_idx.join(pub, on="day").with_columns(err=(pl.col("value") / pl.col("true")).log().abs())
    assert j.height >= 10
    # measured on this fixture: mean ~1.2%, worst day ~3.6% (docs/PERFORMANCE.md, "Accuracy")
    assert j["err"].mean() < 0.02 and j["err"].max() < 0.05, j.sort("err", descending=True).head(3)


def test_injected_manipulation_is_detected(seeded, small_world):
    truth = pl.read_parquet(small_world["root"] / "truth" / "manipulations.parquet").filter(
        pl.col("kind").is_in(["absurd_listing", "bait_listing"])
    )
    events = pl.DataFrame(
        q(
            seeded["pipeline"],
            """SELECT server_id, item_id, day FROM manipulation_event
                                                  WHERE kind = 'extreme_listing'""",
        ),
        schema=["server_id", "item_id", "day"],
        orient="row",
    )
    hits = truth.join(events, on=["server_id", "item_id", "day"], how="semi")
    assert truth.height > 0
    assert hits.height / truth.height >= 0.9, (hits.height, truth.height)


def test_real_eve_manipulation_is_rejected_not_published(seeded):
    """The profile found 0.01 ISK trade days in thin regions; none may reach a published price."""
    rows = q(
        seeded["pipeline"],
        """SELECT count(*) FROM item_price_current p JOIN server s USING (server_id)
                                    WHERE s.world_id = 'eve' AND p.status = 'ok' AND p.price <= 0.02""",
    )
    assert rows[0][0] == 0


# ------------------------------------------------------------------------------------------ reproducibility
def test_rerun_with_unchanged_inputs_writes_nothing(seeded):
    with db.connect(seeded["cfg"]) as conn:
        report = seeded["pipe"].run(conn, "synthetic")
    assert report.days_processed == [] and report.prices_written == 0 and report.index_written == 0


def test_index_is_reproducible_from_raw_into_a_fresh_database(seeded, pg, tmp_path):
    shutil.copytree(seeded["data"] / "raw" / "synthetic", tmp_path / "raw" / "synthetic")
    with fresh_db(pg) as d:
        cfg = make_settings(tmp_path, d)
        with db.connect(cfg) as conn:
            Pipeline(cfg).run(conn, "synthetic")
        sql = """SELECT coalesce(s.server_id,''), coalesce(s.division_id,''), v.day, v.value, v.coverage, v.status
                 FROM index_value_current v JOIN index_series s USING (series_id) WHERE s.world_id='synthetic' ORDER BY 1,2,3"""
        assert q(d["pipeline"], sql) == q(seeded["pipeline"], sql)  # bit-for-bit
        psql = """SELECT server_id, item_id, day, price, volume, status FROM item_price_current
                  WHERE server_id LIKE 'syn-%%' ORDER BY 1,2,3"""
        assert q(d["pipeline"], psql) == q(seeded["pipeline"], psql)


def test_late_data_creates_visible_revisions_and_reprocesses_only_affected_days(pg, tmp_path):
    store = RawStore(tmp_path / "raw")
    cfg_s = small_config()
    synthetic.generate(cfg_s, store, REFERENCE, until=datetime(2025, 10, 10, tzinfo=UTC))
    before = {
        (d, r.path.name) for d in store.days("synthetic", "orders") for r in store.iter_refs("synthetic", "orders", d)
    }
    with fresh_db(pg) as d:
        cfg = make_settings(tmp_path, d)
        pipe = Pipeline(cfg)
        with db.connect(cfg) as conn:
            pipe.run(conn, "synthetic")
            t_between = conn.execute("SELECT now()").fetchone()[0]
        synthetic.generate(cfg_s, store, REFERENCE, until=datetime(2025, 10, 16, tzinfo=UTC))
        after = {
            (d, r.path.name)
            for d in store.days("synthetic", "orders")
            for r in store.iter_refs("synthetic", "orders", d)
        }
        late_days = sorted({day for day, _ in after - before if day < date(2025, 10, 9)})
        assert late_days, "the generator must have produced late arrivals for this window"
        with db.connect(cfg) as conn:
            changed = pipe.changed_days(conn, "synthetic")
            allowed = (
                set(late_days)
                | {x + timedelta(days=1) for x in late_days}
                | {date(2025, 10, 9) + timedelta(days=k) for k in range(8)}
            )
            assert set(late_days) <= set(changed) <= allowed  # targeted: nothing else is recomputed
            pipe.run(conn, "synthetic")
        revised = q(d["pipeline"], """SELECT DISTINCT day, reason FROM item_price_daily WHERE vintage > 1""")
        assert revised and {r for _, r in revised} == {"late_data"}
        assert {day for day, _ in revised} <= allowed
        # the first vintage is still there and an as-of query reproduces exactly what was published then
        old = q(
            d["pipeline"],
            """SELECT DISTINCT ON (day) day, value FROM index_value v JOIN index_series s USING (series_id)
                                  WHERE s.world_id='synthetic' AND s.server_id IS NULL AND s.division_id IS NULL
                                    AND computed_at <= %s ORDER BY day, vintage DESC""",
            (t_between,),
        )
        firsts = q(
            d["pipeline"],
            """SELECT day, value FROM index_value v JOIN index_series s USING (series_id)
                                     WHERE s.world_id='synthetic' AND s.server_id IS NULL AND s.division_id IS NULL
                                       AND vintage = 1 AND computed_at <= %s ORDER BY day""",
            (t_between,),
        )
        assert old == firsts
