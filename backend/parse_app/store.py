"""Database reads/writes shared by the API, the worker and the pipeline CLI."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import numpy as np
from sqlalchemy import Connection, Engine, text

from .db import bulk_insert
from .embed import Embedder, Vecs
from .hier import HierModel
from .taxonomy import Bools, Tree, current_version

EMBED_CHUNK = 2048


class StoreError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def to_vecs(blobs: Sequence[bytes], dim: int = 384) -> Vecs:
    return np.frombuffer(b"".join(blobs), dtype="<f4").reshape(-1, dim)


# --- embeddings ---------------------------------------------------------------------------------


def embed_missing(engine: Engine, embedder: Embedder, model_name: str, progress: Any = None) -> int:
    """Embed every feedback row that has no embedding yet. Idempotent and resumable: each chunk
    commits on its own, so a crash loses at most one chunk of work."""
    with engine.connect() as conn:
        total = conn.execute(
            text(
                "SELECT count(*) FROM feedback f LEFT JOIN embeddings e ON e.feedback_id = f.id WHERE e.feedback_id IS NULL"
            )
        ).scalar_one()
    done = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    """SELECT f.id, f.text FROM feedback f LEFT JOIN embeddings e ON e.feedback_id = f.id
                       WHERE e.feedback_id IS NULL ORDER BY f.id LIMIT :n"""
                ),
                {"n": EMBED_CHUNK},
            ).all()
            if not rows:
                return done
            vecs = embedder.embed([r.text for r in rows]).astype("<f4")
            bulk_insert(
                conn, "embeddings", {"feedback_id": "bigint", "model": "text", "dim": "smallint", "vec": "bytea"},
                [{"feedback_id": r.id, "model": model_name, "dim": vecs.shape[1], "vec": v.tobytes()}
                 for r, v in zip(rows, vecs, strict=True)],
                "ON CONFLICT (feedback_id) DO NOTHING",
            )  # fmt: skip
        done += len(rows)
        if progress:
            progress(done / max(total, 1))


# --- labels -------------------------------------------------------------------------------------


def save_annotation(
    conn: Connection, feedback_id: int, node_ids: Sequence[int], annotator: str, source: str = "human",
    allow_reference: bool = False,
) -> dict[str, Any]:  # fmt: skip
    """Replace the label set of an item. A PUT in the HTTP sense: repeating it is a no-op.
    The ancestor closure is added by the database trigger, not here.

    Evaluation items are protected: their reference labels can only be replaced by a caller with
    `allow_reference` (an admin), and only while a taxonomy change has them flagged for review -
    the reference set needs the same targeted refresh as the training labels."""
    row = conn.execute(
        text(
            """SELECT split, EXISTS (SELECT 1 FROM labels l WHERE l.feedback_id = f.id AND l.review_reason IS NOT NULL) AS flagged
               FROM feedback f WHERE id = :f FOR UPDATE"""
        ),
        {"f": feedback_id},
    ).one_or_none()
    if row is None:
        raise StoreError(f"feedback item {feedback_id} does not exist", 404)
    if row.split != "pool":
        if not (allow_reference and row.flagged):
            raise StoreError(
                "evaluation items carry reference labels and cannot be relabelled, except by an admin "
                "while a taxonomy change has the item flagged for review", 409,
            )  # fmt: skip
        source = "gold"
    wanted = sorted(set(node_ids))
    active = set(
        conn.execute(
            text("SELECT id FROM taxonomy_nodes WHERE id = ANY(:ids) AND retired_version IS NULL"), {"ids": wanted}
        ).scalars()
    )
    unknown = [n for n in wanted if n not in active]
    if unknown:
        raise StoreError(f"unknown or retired taxonomy node ids: {unknown}", 422)
    model_id = conn.execute(text("SELECT id FROM model_versions WHERE status = 'active'")).scalar()
    conn.execute(
        text(
            """INSERT INTO annotations (feedback_id, annotator, source, taxonomy_version, model_version_id)
               VALUES (:f, :a, :s, :v, :m)
               ON CONFLICT (feedback_id) DO UPDATE SET annotator = EXCLUDED.annotator, source = EXCLUDED.source,
                   taxonomy_version = EXCLUDED.taxonomy_version, model_version_id = EXCLUDED.model_version_id,
                   annotated_at = now()"""
        ),
        {"f": feedback_id, "a": annotator, "s": source, "v": current_version(conn), "m": model_id},
    )
    conn.execute(text("DELETE FROM labels WHERE feedback_id = :f"), {"f": feedback_id})
    if wanted:
        conn.execute(
            text("INSERT INTO labels (feedback_id, node_id) VALUES (:f, :n) ON CONFLICT DO NOTHING"),
            [{"f": feedback_id, "n": n} for n in wanted],
        )
    final = conn.execute(text("SELECT node_id FROM labels WHERE feedback_id = :f ORDER BY node_id"), {"f": feedback_id})
    return {"feedback_id": feedback_id, "node_ids": list(final.scalars())}


def labelled_sets(conn: Connection, split: str, tree: Tree) -> tuple[list[int], Vecs, Bools, list[str], list[str]]:
    """(ids, x, y, lang, group) for annotated, non-duplicate items of a split.
    `split='pool'` reads human/simulated labels only; `split='test'` reads gold only."""
    sources = ["human", "simulated"] if split == "pool" else ["gold"]
    rows = conn.execute(
        text(
            """SELECT f.id, f.lang, f.group_key, e.vec,
                      COALESCE(array_agg(l.node_id) FILTER (WHERE l.node_id IS NOT NULL), '{}') AS nodes
               FROM annotations a
               JOIN feedback f ON f.id = a.feedback_id
               JOIN embeddings e ON e.feedback_id = f.id
               LEFT JOIN labels l ON l.feedback_id = f.id
               WHERE f.split = :split AND f.duplicate_of IS NULL AND a.source = ANY(:sources)
               GROUP BY f.id, e.vec ORDER BY f.id"""
        ),
        {"split": split, "sources": sources},
    ).all()
    if not rows:
        return [], np.empty((0, 384), dtype=np.float32), np.zeros((0, len(tree)), dtype=bool), [], []
    return (
        [r.id for r in rows], to_vecs([r.vec for r in rows]), tree.encode([r.nodes for r in rows]),
        [r.lang or "und" for r in rows], [r.group_key for r in rows],
    )  # fmt: skip


def unlabelled_pool(conn: Connection) -> tuple[list[int], Vecs]:
    rows = conn.execute(
        text(
            """SELECT f.id, e.vec FROM feedback f JOIN embeddings e ON e.feedback_id = f.id
               LEFT JOIN annotations a ON a.feedback_id = f.id
               WHERE f.split = 'pool' AND f.duplicate_of IS NULL AND a.feedback_id IS NULL ORDER BY f.id"""
        )
    ).all()
    return [r.id for r in rows], (to_vecs([r.vec for r in rows]) if rows else np.empty((0, 384), dtype=np.float32))


def pool_mean(conn: Connection) -> Any:
    """Mean embedding of the whole pool: an unsupervised statistic, so using unlabelled data is fine."""
    rows = (
        conn.execute(
            text("SELECT e.vec FROM embeddings e JOIN feedback f ON f.id = e.feedback_id WHERE f.split = 'pool'")
        )
        .scalars()
        .all()
    )
    return to_vecs(rows).astype(np.float64).mean(axis=0)


# --- models -------------------------------------------------------------------------------------


def active_model(conn: Connection) -> tuple[int, HierModel] | None:
    row = conn.execute(text("SELECT id, artifact FROM model_versions WHERE status = 'active'")).one_or_none()
    return None if row is None else (row.id, HierModel.from_bytes(bytes(row.artifact)))


# --- jobs ---------------------------------------------------------------------------------------


def enqueue_job(conn: Connection, kind: str, key: str, requested_by: str) -> dict[str, Any]:
    """Idempotent: the same key returns the job that already exists."""
    conn.execute(
        text(
            """INSERT INTO jobs (kind, idempotency_key, requested_by) VALUES (:k, :key, :by)
               ON CONFLICT (idempotency_key) DO NOTHING"""
        ),
        {"k": kind, "key": key, "by": requested_by},
    )
    return dict(
        conn.execute(text(f"SELECT {JOB_COLS} FROM jobs WHERE idempotency_key = :key"), {"key": key}).one()._asdict()
    )


JOB_COLS = (
    "id, kind, status, progress, stage, attempts, error, result, requested_by, requested_at, started_at, finished_at"
)


def set_progress(engine: Engine, job_id: int | None, progress: float, stage: str) -> None:
    if job_id is None:
        return
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET progress = :p, stage = :s WHERE id = :id"),
            {"p": round(min(max(progress, 0.0), 1.0), 3), "s": stage, "id": job_id},
        )


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=float)


# --- labelling queue ----------------------------------------------------------------------------

ITEM_COLS = "f.id, f.text, f.lang, f.source, f.created_at, f.is_late, f.split"


def queue_page(
    conn: Connection, *, limit: int, after: tuple[float, int] | None = None, lang: str | None = None
) -> list[Any]:
    """Unlabelled pool items, most worth labelling first. Keyset-paginated on (priority DESC, id)."""
    prio, fid = after if after else (None, None)
    return list(
        conn.execute(
            text(
                f"""SELECT {ITEM_COLS}, p.node_ids, p.probs, p.confidence, p.uncertainty, p.priority, p.model_version_id
                    FROM predictions p JOIN feedback f ON f.id = p.feedback_id
                    WHERE NOT EXISTS (SELECT 1 FROM annotations a WHERE a.feedback_id = p.feedback_id)
                      AND f.split = 'pool' AND f.duplicate_of IS NULL
                      AND (CAST(:lang AS text) IS NULL OR f.lang = :lang)
                      AND (CAST(:prio AS real) IS NULL OR p.priority < CAST(:prio AS real)
                           OR (p.priority = CAST(:prio AS real) AND p.feedback_id > :fid))
                    ORDER BY p.priority DESC, p.feedback_id LIMIT :n"""  # noqa: S608 - constant column list
            ),
            {"lang": lang, "prio": prio, "fid": fid, "n": limit},
        )
    )


def review_page(conn: Connection, *, limit: int, after_id: int = 0, lang: str | None = None) -> list[Any]:
    """Items a taxonomy change flagged for targeted relabelling (pool and evaluation), with their current labels."""
    return list(
        conn.execute(
            text(
                f"""SELECT {ITEM_COLS}, e.vec, min(l.review_reason) FILTER (WHERE l.review_reason IS NOT NULL) AS review_reason,
                           array_agg(l.node_id ORDER BY l.node_id) AS current_node_ids
                    FROM feedback f JOIN labels l ON l.feedback_id = f.id JOIN embeddings e ON e.feedback_id = f.id
                    WHERE f.id IN (SELECT feedback_id FROM labels WHERE review_reason IS NOT NULL AND feedback_id > :after)
                      AND (CAST(:lang AS text) IS NULL OR f.lang = :lang)
                    GROUP BY f.id, e.vec ORDER BY f.id LIMIT :n"""  # noqa: S608 - constant column list
            ),
            {"after": after_id, "lang": lang, "n": limit},
        )
    )
