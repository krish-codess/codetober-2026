"""Integration + data tests against a real PostgreSQL. A pipeline that runs green but writes
wrong data must fail here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from parse_app import taxonomy, train, worker
from parse_app.config import get_settings
from parse_app.ingest import ingest_file
from parse_app.pipeline import seed, stage_feed
from parse_app.store import enqueue_job, queue_page, save_annotation

pytestmark = pytest.mark.db
BASELINE = json.loads((Path(__file__).parent / "baseline.json").read_text())


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


# --- row counts, uniqueness, referential integrity ---------------------------------------------------


def test_every_raw_line_is_accounted_for(engine: Engine, seeded: dict[str, Any]) -> None:
    raw = scalar(engine, "SELECT count(*) FROM raw_feedback")
    assert raw == scalar(engine, "SELECT count(*) FROM feedback") + scalar(engine, "SELECT count(*) FROM quarantine")
    assert raw == seeded["ingest"]["n_records"] > 2500
    assert scalar(engine, "SELECT count(*) FROM feedback f JOIN quarantine q USING (batch_id, line_no)") == 0
    b = seeded["ingest"]
    assert b["n_accepted"] + b["n_quarantined"] == b["n_records"]


def test_quarantine_holds_each_injected_defect_with_a_reason(engine: Engine, seeded: dict[str, Any]) -> None:
    reasons = seeded["ingest"]["quarantined_by_reason"]
    assert {"duplicate_delivery", "bad_timestamp", "empty_text"} <= set(reasons)
    assert 0 < sum(reasons.values()) < 0.05 * seeded["ingest"]["n_records"]
    assert scalar(engine, "SELECT count(*) FROM quarantine WHERE btrim(detail) = ''") == 0


def test_uniqueness_and_split_isolation(engine: Engine, seeded: dict[str, Any]) -> None:
    assert (
        scalar(
            engine, "SELECT count(*) FROM (SELECT 1 FROM feedback GROUP BY source, external_id HAVING count(*) > 1) d"
        )
        == 0
    )
    assert (
        scalar(
            engine,
            "SELECT count(*) FROM (SELECT 1 FROM feedback GROUP BY group_key HAVING count(DISTINCT split) > 1) d",
        )
        == 0
    )
    # no text appears on both sides of the split
    assert scalar(engine, """SELECT count(*) FROM feedback a JOIN feedback b ON a.text_sha256 = b.text_sha256
                             WHERE a.split = 'pool' AND b.split = 'test' AND a.lang IS NOT DISTINCT FROM b.lang""") == 0  # fmt: skip
    with engine.connect() as conn:  # and the database itself refuses to let a group change sides
        group = conn.execute(text("SELECT group_key FROM split_groups WHERE split = 'test' LIMIT 1")).scalar_one()
        with pytest.raises(IntegrityError):
            conn.execute(text("UPDATE feedback SET split = 'pool' WHERE group_key = :g"), {"g": group})


def test_raw_feedback_is_immutable(engine: Engine, seeded: dict[str, Any]) -> None:
    for sql in ("UPDATE raw_feedback SET payload = ''", "DELETE FROM raw_feedback", "TRUNCATE raw_feedback CASCADE"):
        with engine.connect() as conn, pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(sql))


def test_test_split_is_parallel_and_gold_only(engine: Engine, seeded: dict[str, Any]) -> None:
    assert (
        scalar(
            engine,
            "SELECT count(DISTINCT n) FROM (SELECT count(*) n FROM feedback WHERE split='test' GROUP BY group_key) g",
        )
        == 1
    )
    assert scalar(engine, "SELECT count(*) FROM annotations a JOIN feedback f ON f.id = a.feedback_id "
                          "WHERE (f.split = 'test') <> (a.source = 'gold')") == 0  # fmt: skip
    assert scalar(engine, "SELECT count(*) FROM feedback f LEFT JOIN annotations a ON a.feedback_id = f.id "
                          "WHERE f.split = 'test' AND a.feedback_id IS NULL") == 0  # fmt: skip


def test_embeddings_cover_everything_and_are_unit_length(engine: Engine, seeded: dict[str, Any]) -> None:
    assert (
        scalar(
            engine,
            "SELECT count(*) FROM feedback f LEFT JOIN embeddings e ON e.feedback_id = f.id WHERE e.feedback_id IS NULL",
        )
        == 0
    )
    assert scalar(engine, "SELECT count(*) FROM embeddings WHERE octet_length(vec) <> 384 * 4") == 0
    with engine.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text("UPDATE embeddings SET vec = '\\x00' WHERE feedback_id = (SELECT min(feedback_id) FROM embeddings)")
        )


def test_distribution_invariants(engine: Engine, seeded: dict[str, Any]) -> None:
    with engine.connect() as conn:
        langs = dict(
            conn.execute(text("SELECT COALESCE(lang,'und'), count(*) FROM feedback WHERE split='pool' GROUP BY 1"))
            .tuples()
            .all()
        )
    total = sum(langs.values())
    assert langs["en"] / total > 0.45 and 0.01 < langs["sw"] / total < 0.10  # head and tail of the language mix
    assert 0.01 < langs["und"] / total < 0.06  # injected missing-lang rate
    assert seeded["ingest"]["n_late"] > 0 and seeded["ingest"]["n_text_duplicates"] > 0
    assert seeded["ingest"]["n_test_overlap"] == 0
    assert scalar(engine, "SELECT count(*) FROM feedback WHERE is_late AND created_at IS NULL") == 0


def test_text_duplicates_are_kept_but_never_queued(engine: Engine, seeded: dict[str, Any]) -> None:
    assert (
        scalar(
            engine,
            "SELECT count(*) FROM feedback d JOIN feedback o ON o.id = d.duplicate_of WHERE d.text_sha256 <> o.text_sha256",
        )
        == 0
    )
    with engine.connect() as conn:
        queued = {r.id for r in queue_page(conn, limit=100)}
        dupes = set(conn.execute(text("SELECT id FROM feedback WHERE duplicate_of IS NOT NULL")).scalars())
    assert queued and not queued & dupes


def test_ingest_is_idempotent(engine: Engine, seeded: dict[str, Any]) -> None:
    before = scalar(engine, "SELECT count(*) FROM raw_feedback")
    again = ingest_file(engine, stage_feed(get_settings())[0])
    assert again["already_ingested"] and again["n_records"] == seeded["ingest"]["n_records"]
    assert scalar(engine, "SELECT count(*) FROM raw_feedback") == before
    assert seed(engine, get_settings())["embedded"] == 0  # whole seed is a no-op the second time
    assert scalar(engine, "SELECT count(*) FROM ingest_batches") == 1


# --- hierarchical consistency in the database --------------------------------------------------------

ORPHANS = """SELECT count(*) FROM labels l JOIN taxonomy_nodes n ON n.id = l.node_id
             WHERE n.parent_id IS NOT NULL AND NOT EXISTS (
                 SELECT 1 FROM labels p WHERE p.feedback_id = l.feedback_id AND p.node_id = n.parent_id)"""


def test_no_label_exists_without_its_parent(engine: Engine, seeded: dict[str, Any]) -> None:
    assert scalar(engine, "SELECT count(*) FROM labels") > 1000
    assert scalar(engine, ORPHANS) == 0


def test_triggers_maintain_closure_on_insert_and_delete(engine: Engine, seeded: dict[str, Any]) -> None:
    with engine.connect() as conn:  # rolled back at exit
        leaf = conn.execute(text("SELECT id, parent_id FROM taxonomy_nodes WHERE path = 'hotel/rooms/comfort'")).one()
        root = conn.execute(text("SELECT id FROM taxonomy_nodes WHERE path = 'hotel'")).scalar_one()
        item = conn.execute(text("SELECT f.id FROM feedback f LEFT JOIN annotations a ON a.feedback_id = f.id "
                                 "WHERE f.split = 'pool' AND a.feedback_id IS NULL LIMIT 1")).scalar_one()  # fmt: skip
        # Raw SQL, bypassing the application entirely: the trigger still closes the set.
        conn.execute(
            text(
                "INSERT INTO annotations (feedback_id, annotator, source, taxonomy_version) VALUES (:f, 't', 'human', 1)"
            ),
            {"f": item},
        )
        conn.execute(text("INSERT INTO labels (feedback_id, node_id) VALUES (:f, :n)"), {"f": item, "n": leaf.id})
        got = set(conn.execute(text("SELECT node_id FROM labels WHERE feedback_id = :f"), {"f": item}).scalars())
        assert got == {leaf.id, leaf.parent_id, root}
        conn.execute(text("DELETE FROM labels WHERE feedback_id = :f AND node_id = :n"), {"f": item, "n": root})
        assert conn.execute(text("SELECT count(*) FROM labels WHERE feedback_id = :f"), {"f": item}).scalar() == 0


def test_save_annotation_is_idempotent_and_guards_the_test_split(engine: Engine, seeded: dict[str, Any]) -> None:
    from parse_app.store import StoreError

    with engine.connect() as conn:
        leaf = conn.execute(text("SELECT id FROM taxonomy_nodes WHERE path = 'laptop/display/quality'")).scalar_one()
        item = conn.execute(text("SELECT f.id FROM feedback f LEFT JOIN annotations a ON a.feedback_id = f.id "
                                 "WHERE f.split = 'pool' AND a.feedback_id IS NULL LIMIT 1")).scalar_one()  # fmt: skip
        first = save_annotation(conn, item, [leaf], "t")
        assert len(first["node_ids"]) == 3 and save_annotation(conn, item, [leaf], "t") == first
        assert save_annotation(conn, item, [], "t")["node_ids"] == []  # "nothing applies" is a valid answer
        assert conn.execute(text("SELECT count(*) FROM annotations WHERE feedback_id = :f"), {"f": item}).scalar() == 1
        test_item = conn.execute(text("SELECT id FROM feedback WHERE split = 'test' LIMIT 1")).scalar_one()
        for allow in (False, True):  # even an admin cannot touch a reference item that is not flagged
            with pytest.raises(StoreError, match="cannot be relabelled"):
                save_annotation(conn, test_item, [leaf], "t", allow_reference=allow)
        with pytest.raises(StoreError, match="unknown or retired"):
            save_annotation(conn, item, [10**6], "t")


# --- model lifecycle ---------------------------------------------------------------------------------


def test_seed_trained_versioned_models(engine: Engine, seeded: dict[str, Any]) -> None:
    rounds = seeded["bootstrap"]
    assert [r["n_labeled"] for r in rounds] == sorted(r["n_labeled"] for r in rounds) and rounds[-1]["n_labeled"] == 250
    assert scalar(engine, "SELECT count(*) FROM model_versions WHERE status = 'active'") == 1
    with engine.connect() as conn:
        m = conn.execute(text("SELECT * FROM model_versions WHERE status = 'active'")).one()
    assert m.metrics["consistent"] == 1.0 and m.metrics["n"] > 1000
    assert set(m.metrics["by_lang"]) == {"en", "es", "de", "sw"}
    assert len(m.train_data_sha256) == 64 and m.params["c"] > 0 and m.embed_model == "hash-ngram-384"
    assert scalar(engine, "SELECT count(*) FROM node_metrics WHERE model_version_id = :m AND lang = 'all'", m=m.id) > 10
    with engine.connect() as conn, pytest.raises(IntegrityError):  # at most one active model, ever
        conn.execute(text("UPDATE model_versions SET status = 'active' WHERE status = 'archived'"))


def test_evaluation_regression_gate(engine: Engine, seeded: dict[str, Any]) -> None:
    """Fails if a code change costs more than 0.03 hF1 on the deterministic synthetic benchmark."""
    hf1 = scalar(engine, "SELECT (metrics->>'hf1')::float FROM model_versions WHERE status = 'active'")
    assert hf1 >= BASELINE["synthetic_hf1_at_250_labels"] - 0.03, f"hF1 {hf1:.3f} regressed vs committed baseline"


def test_active_learning_queue_is_ordered_and_excludes_labelled(engine: Engine, seeded: dict[str, Any]) -> None:
    with engine.connect() as conn:
        page = queue_page(conn, limit=50)
        prios = [r.priority for r in page]
        assert prios == sorted(prios, reverse=True) and prios[0] > 1.0  # diversified head of the queue
        nxt = queue_page(conn, limit=50, after=(page[-1].priority, page[-1].id))
        assert not {r.id for r in page} & {r.id for r in nxt}
        labelled = set(conn.execute(text("SELECT feedback_id FROM annotations")).scalars())
        assert not {r.id for r in page + nxt} & labelled
        assert all(len(r.node_ids) == len(r.probs) and 0 <= r.confidence <= 1 for r in page)


def test_promotion_gate_rejects_a_worse_candidate(engine: Engine, seeded: dict[str, Any]) -> None:
    with engine.begin() as conn:
        active = conn.execute(text("SELECT id, metrics FROM model_versions WHERE status = 'active'")).one()
        conn.execute(
            text("UPDATE model_versions SET metrics = jsonb_set(metrics, '{hf1}', '0.999') WHERE id = :id"),
            {"id": active.id},
        )
    try:
        out = train.train(engine, get_settings())
        assert not out["promoted"] and "fell" in out["gate"]["reason"]
        assert scalar(engine, "SELECT id FROM model_versions WHERE status = 'active'") == active.id
        assert scalar(engine, "SELECT status FROM model_versions WHERE id = :id", id=out["model_version"]) == "rejected"
    finally:
        with engine.begin() as conn:
            conn.execute(text("UPDATE model_versions SET metrics = CAST(:m AS jsonb) WHERE id = :id"),
                         {"m": json.dumps(active.metrics), "id": active.id})  # fmt: skip


# --- worker: failure injection -----------------------------------------------------------------------


def test_worker_runs_a_job_and_reports_progress(engine: Engine, seeded: dict[str, Any]) -> None:
    with engine.begin() as conn:
        job = enqueue_job(conn, "retrain", "test-ok-1", "tester")
        assert enqueue_job(conn, "retrain", "test-ok-1", "tester")["id"] == job["id"]  # idempotent
    assert worker.work_one(engine, get_settings())
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job["id"]}).one()
    assert row.status == "succeeded" and row.progress == 1 and row.result["model_version"] and row.finished_at
    assert not worker.work_one(engine, get_settings())  # queue drained


def test_worker_retries_then_fails_without_losing_the_serving_model(
    engine: Engine, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    active_before = scalar(engine, "SELECT id FROM model_versions WHERE status = 'active'")

    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(worker, "train", boom)
    with engine.begin() as conn:
        job = enqueue_job(conn, "retrain", "test-fail-1", "tester")
    for attempt in range(1, worker.MAX_ATTEMPTS + 1):
        assert worker.work_one(engine, get_settings())
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT status, attempts, error, requested_at > now() AS delayed FROM jobs WHERE id = :id"),
                {"id": job["id"]},
            ).one()
            assert row.attempts == attempt and "disk full" in row.error
            if attempt < worker.MAX_ATTEMPTS:
                assert row.status == "queued" and row.delayed  # backoff: not runnable yet
                assert not worker.work_one(engine, get_settings())
                conn.execute(text("UPDATE jobs SET requested_at = now() WHERE id = :id"), {"id": job["id"]})
    assert row.status == "failed"  # capped
    assert scalar(engine, "SELECT id FROM model_versions WHERE status = 'active'") == active_before


def test_scheduler_enqueues_once_per_state(
    engine: Engine, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    assert worker.schedule_retrain(engine, settings) is None  # nothing new since the active model
    worker.simulate = __import__("parse_app.pipeline", fromlist=["simulate"]).simulate  # type: ignore[attr-defined]
    assert worker.simulate(engine, settings.retrain_min_new_labels) == settings.retrain_min_new_labels  # type: ignore[attr-defined]
    first = worker.schedule_retrain(engine, settings)
    assert first and first["status"] == "queued"
    assert worker.schedule_retrain(engine, settings)["id"] == first["id"]  # type: ignore[index]
    assert worker.work_one(engine, settings)
    assert worker.schedule_retrain(engine, settings) is None


# --- taxonomy changes --------------------------------------------------------------------------------


def _labels(engine: Engine, item: int) -> dict[str, str | None]:
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT n.path, l.review_reason FROM labels l JOIN taxonomy_nodes n ON n.id = l.node_id "
                                      "WHERE l.feedback_id = :f"), {"f": item}).tuples().all())  # fmt: skip


def _pick(engine: Engine, path: str, n: int) -> list[int]:
    """n pool items whose most specific human label is `path`."""
    with engine.connect() as conn:
        return list(conn.execute(text(
            """SELECT l.feedback_id FROM labels l JOIN taxonomy_nodes t ON t.id = l.node_id
               JOIN feedback f ON f.id = l.feedback_id
               WHERE t.path = :p AND f.split = 'pool' ORDER BY l.feedback_id LIMIT :n"""), {"p": path, "n": n}).scalars())  # fmt: skip


def test_split_flags_only_the_affected_items_and_keeps_every_label(engine: Engine, seeded: dict[str, Any]) -> None:
    total_before = scalar(engine, "SELECT count(*) FROM labels")
    on_node = scalar(
        engine,
        "SELECT count(*) FROM labels l JOIN taxonomy_nodes n ON n.id = l.node_id WHERE n.path = 'hotel/rooms/comfort'",
    )
    with engine.begin() as conn:
        out = taxonomy.split(conn, path="hotel/rooms/comfort", key="split-0001", actor="t",
                             children=[{"name": "bed", "title": "Bed"}, {"name": "noise", "title": "Noise"}])  # fmt: skip
    assert out["labels_flagged"] == on_node > 0 and not out["replayed"]
    assert scalar(engine, "SELECT count(*) FROM labels") == total_before  # nothing discarded
    assert (
        scalar(engine, "SELECT count(*) FROM labels WHERE review_reason IS NOT NULL") == on_node
    )  # nothing else touched
    assert scalar(engine, ORPHANS) == 0
    with engine.begin() as conn:  # replay with the same key: no second version, same answer
        again = taxonomy.split(conn, path="hotel/rooms/comfort", key="split-0001", actor="t",
                               children=[{"name": "bed", "title": "Bed"}, {"name": "noise", "title": "Noise"}])  # fmt: skip
    assert again["replayed"] and again["version"] == out["version"]
    # resolving a flagged item through the normal labelling path clears its flag
    item = _pick(engine, "hotel/rooms/comfort", 1)[0]
    with engine.begin() as conn:
        bed = conn.execute(text("SELECT id FROM taxonomy_nodes WHERE path = 'hotel/rooms/comfort/bed'")).scalar_one()
        save_annotation(conn, item, [bed], "t")
    assert _labels(engine, item) == {
        "hotel": None,
        "hotel/rooms": None,
        "hotel/rooms/comfort": None,
        "hotel/rooms/comfort/bed": None,
    }


def test_merge_remaps_labels_without_any_review(engine: Engine, seeded: dict[str, Any]) -> None:
    src_items = _pick(engine, "restaurant/food/prices", 500)
    flagged_before = scalar(engine, "SELECT count(*) FROM labels WHERE review_reason IS NOT NULL")
    with engine.begin() as conn:
        out = taxonomy.merge(
            conn, source="restaurant/food/prices", target="restaurant/food/quality", key="merge-0001", actor="t"
        )
    assert out["labels_flagged"] == 0 and out["labels_remapped"] >= len(src_items) > 0  # pool + reference labels
    assert scalar(engine, "SELECT count(*) FROM labels WHERE review_reason IS NOT NULL") == flagged_before
    assert "restaurant/food/quality" in _labels(engine, src_items[0]) and "restaurant/food/prices" not in _labels(
        engine, src_items[0]
    )
    assert (
        scalar(engine, "SELECT retired_version FROM taxonomy_nodes WHERE path = 'restaurant/food/prices'")
        == out["version"]
    )
    assert scalar(engine, ORPHANS) == 0
    with engine.connect() as conn, pytest.raises(DBAPIError, match="retired"):
        retired = conn.execute(text("SELECT id FROM taxonomy_nodes WHERE path = 'restaurant/food/prices'")).scalar_one()
        conn.execute(
            text("INSERT INTO labels (feedback_id, node_id) VALUES (:f, :n)"), {"f": src_items[0], "n": retired}
        )


def test_rename_cascades_paths_and_touches_no_label(engine: Engine, seeded: dict[str, Any]) -> None:
    before = scalar(engine, "SELECT count(*) FROM labels")
    with engine.begin() as conn:
        out = taxonomy.rename(conn, path="laptop/battery", name="power", title="Power", key="rename-0001", actor="t")
    assert (
        out["labels_remapped"] == out["labels_flagged"] == 0 and scalar(engine, "SELECT count(*) FROM labels") == before
    )
    assert scalar(engine, "SELECT count(*) FROM taxonomy_nodes WHERE path = 'laptop/power/operation_performance'") == 1
    assert (
        scalar(
            engine, "SELECT count(*) FROM taxonomy_nodes WHERE retired_version IS NULL AND path LIKE 'laptop/battery%'"
        )
        == 0
    )


def test_move_recloses_labels_and_rejects_cycles(engine: Engine, seeded: dict[str, Any]) -> None:
    items = _pick(engine, "hotel/rooms/cleanliness", 5)
    with engine.begin() as conn, pytest.raises(taxonomy.TaxonomyError, match="already has a child"):
        taxonomy.move(conn, path="hotel/location/general", new_parent="hotel/service", key="move-0000", actor="t")
    with engine.begin() as conn:
        out = taxonomy.move(
            conn, path="hotel/rooms/cleanliness", new_parent="hotel/service", key="move-0001", actor="t"
        )
    for item in items:
        assert {"hotel/service/cleanliness", "hotel/service", "hotel"} <= set(_labels(engine, item))
    assert scalar(engine, ORPHANS) == 0
    # the old parent is no longer implied: where nothing else justifies it, ask a person
    moved = scalar(engine, "SELECT count(*) FROM labels WHERE review_reason = 'moved:hotel/rooms/cleanliness'")
    assert moved == out["labels_flagged"] > 0
    with engine.begin() as conn, pytest.raises(taxonomy.TaxonomyError, match="descendant"):
        taxonomy.move(conn, path="hotel", new_parent="hotel/rooms", key="move-0002", actor="t")
    with engine.connect() as conn, pytest.raises(DBAPIError, match="cycle"):  # and the database agrees
        conn.execute(text("UPDATE taxonomy_nodes SET parent_id = (SELECT id FROM taxonomy_nodes WHERE path = 'hotel/rooms') "
                          "WHERE path = 'hotel'"))  # fmt: skip


def test_retire_falls_back_to_the_parent_and_add_rejects_clashes(engine: Engine, seeded: dict[str, Any]) -> None:
    items = _pick(engine, "laptop/display/quality", 3)
    with engine.begin() as conn:
        out = taxonomy.retire(conn, path="laptop/display", key="retire-0001", actor="t")
    assert out["labels_remapped"] > 0
    after = set(_labels(engine, items[0]))
    assert "laptop" in after and not any(p.startswith("laptop/display") for p in after)
    assert scalar(engine, "SELECT count(*) FROM taxonomy_nodes WHERE retired_version = :v", v=out["version"]) == 2
    with engine.begin() as conn, pytest.raises(taxonomy.TaxonomyError, match="already exists"):
        taxonomy.add(conn, parent="laptop", name="keyboard", title="Keyboard", key="add-0001", actor="t")
    with engine.begin() as conn, pytest.raises(taxonomy.TaxonomyError, match="no active node"):
        taxonomy.retire(conn, path="nope/nothing", key="retire-0002", actor="t")
    # failed operations left no version behind
    assert (
        scalar(
            engine,
            "SELECT count(*) FROM taxonomy_versions WHERE idempotency_key IN ('add-0001','retire-0002','move-0002')",
        )
        == 0
    )


def test_model_retrains_cleanly_on_the_changed_taxonomy(engine: Engine, seeded: dict[str, Any]) -> None:
    out = train.train(engine, get_settings())
    assert out["promoted"] and "taxonomy changed" in out["gate"]["reason"]
    with engine.connect() as conn:
        m = conn.execute(
            text("SELECT metrics FROM model_versions WHERE id = :id"), {"id": out["model_version"]}
        ).scalar_one()
    assert m["consistent"] == 1.0 and m["hf1"] > 0.5
