"""Justify every non-constraint index with the query it serves.

For each index: EXPLAIN (ANALYZE, BUFFERS) the real query, then again inside a transaction that
drops the index and rolls back. Output goes to docs/explain/<index>.txt.

    python -m parse_app.explain     # needs owner credentials (DROP INDEX), i.e. MIGRATION_DATABASE_URL
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

OUT = Path(__file__).resolve().parents[2] / "docs" / "explain"

# (index, why, query, parameter lookups)
CASES: list[tuple[str, str, str, dict[str, str]]] = [
    (
        "predictions_queue",
        "Labelling queue: next 20 items by priority (GET /queue). Runs on every page of labelling.",
        """SELECT f.id, f.text, p.node_ids, p.probs, p.priority
           FROM predictions p JOIN feedback f ON f.id = p.feedback_id
           WHERE f.duplicate_of IS NULL
           ORDER BY p.priority DESC, p.feedback_id LIMIT 20""",
        {},
    ),
    (
        "labels_node",
        "Taxonomy operations and per-node lookups: every label on one node (merge/split/retire).",
        "SELECT feedback_id FROM labels WHERE node_id = :node",
        {"node": "SELECT node_id FROM labels GROUP BY node_id ORDER BY count(*) LIMIT 1 OFFSET 20"},
    ),
    (
        "labels_review",
        "Targeted-relabel queue: items with a flagged label (GET /queue?mode=review).",
        "SELECT DISTINCT feedback_id FROM labels WHERE review_reason IS NOT NULL AND feedback_id > 0 ORDER BY 1 LIMIT 20",
        {},
    ),
    (
        "feedback_text_sha256",
        "Duplicate detection at ingest and the pool/test leakage guard: rows sharing a text hash.",
        "SELECT id, split FROM feedback WHERE text_sha256 = :sha",
        {"sha": "SELECT text_sha256 FROM feedback ORDER BY id LIMIT 1 OFFSET 500"},
    ),
]


def plan(conn: Any, query: str, params: dict[str, Any]) -> str:
    rows = conn.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + query), params).scalars().all()
    return "\n".join(rows)


def main() -> None:
    engine = create_engine(os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"])
    OUT.mkdir(parents=True, exist_ok=True)
    with engine.connect() as conn:
        conn.execute(text("ANALYZE"))
        conn.commit()
        sizes = dict(conn.execute(text("SELECT relname, n_live_tup FROM pg_stat_user_tables")).tuples().all())
        for index, why, query, lookups in CASES:
            params = {k: conn.execute(text(q)).scalar() for k, q in lookups.items()}
            with_index = plan(conn, query, params)
            conn.rollback()
            conn.execute(text(f"DROP INDEX {index}"))  # noqa: S608 - names come from CASES above
            without = plan(conn, query, params)
            conn.rollback()  # the DROP never commits
            body = (
                f"Index: {index}\nServes: {why}\n\nQuery:\n{query}\nParameters: {params}\n"
                f"Table sizes (rows): {{{', '.join(f'{k}: {v}' for k, v in sorted(sizes.items()) if v)}}}\n\n"
                f"--- WITH the index ---\n{with_index}\n\n--- WITHOUT the index (dropped inside a rolled-back transaction) ---\n{without}\n"
            )
            (OUT / f"{index}.txt").write_text(body, encoding="utf-8", newline="\n")
            times = [p.rsplit("Execution Time: ", 1)[-1].split(" ms")[0] for p in (with_index, without)]
            print(f"{index:24s} with={times[0]:>8s} ms   without={times[1]:>8s} ms")


if __name__ == "__main__":
    main()
