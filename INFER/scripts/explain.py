"""Capture EXPLAIN ANALYZE for the queries the system runs, into docs/evidence/explain-analyze.md.

    python scripts/explain.py            (uses UPLIFT_MIGRATE_DATABASE_URL; needs a seeded, scored database)

Each index is justified by the query it serves. The two secondary indexes are also measured against the same query
with the index dropped inside a transaction that is rolled back.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from uplift import config
from uplift.policy import EXPORT

OUT = Path(__file__).resolve().parent.parent / "docs" / "evidence" / "explain-analyze.md"

SERVED = (
    "SELECT e.id FROM experiment e JOIN dataset d ON d.id = e.dataset_id"
    " WHERE d.name = 'synthetic' AND e.status = 'complete' ORDER BY e.finished_at DESC LIMIT 1"
)
QUERIES = [
    (
        "Score page: `GET /v1/runs/{id}/scores`",
        "Keyset pagination served by `score_ranked (run_id, split, uplift, unit_id)`, scanned backward: no sort, no offset.",
        "SELECT unit_id, split, uplift FROM score WHERE run_id = %(run)s AND split = 'test'"
        " AND (uplift, unit_id) < (%(uplift)s, %(unit)s) ORDER BY uplift DESC, unit_id DESC LIMIT 101",
        "score_ranked",
    ),
    (
        "Policy export: `GET /v1/policies/{id}/export`",
        "The top n by predicted uplift, read straight off `score_ranked` in ranking order.",
        EXPORT.replace("%s", "%(run)s", 1).replace("%s", "5000"),
        "score_ranked",
    ),
    (
        "Served experiment: every read endpoint starts here",
        "Served by the partial index `experiment_serving (dataset_id, finished_at DESC) WHERE status = 'complete'`."
        " With a handful of experiments the planner may prefer a sequential scan; the index is for the table after"
        " months of scheduled retraining.",
        SERVED,
        "experiment_serving",
    ),
    (
        "Deciles of a run: `GET /v1/runs/{id}/deciles`",
        "Served by the primary key `decile (run_id, decile)`.",
        "SELECT * FROM decile WHERE run_id = %(run)s ORDER BY decile",
        None,
    ),
    (
        "Unit lookup behind the score foreign key and the policy join",
        "Served by the primary key `unit (dataset_id, unit_id)`.",
        "SELECT treatment, outcome FROM unit WHERE dataset_id = %(dataset)s AND unit_id = %(unit)s",
        None,
    ),
    (
        "Quarantine summary: `GET /v1/datasets`",
        "Joins through the unique constraint `quarantine (raw_sha, line_no)`.",
        "SELECT q.reason, count(*) FROM quarantine q JOIN raw_file f ON f.sha256 = q.raw_sha"
        " WHERE f.dataset_id = %(dataset)s GROUP BY q.reason",
        None,
    ),
]


def plan(conn: psycopg.Connection, sql: str, params: dict[str, object]) -> str:
    rows = conn.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON) " + sql, params).fetchall()
    return "\n".join(r[0] for r in rows)


def main() -> None:
    settings = config.load()
    out = [
        "# EXPLAIN ANALYZE",
        "",
        "Captured by `python scripts/explain.py` against the seeded database. Do not edit by hand.",
        "",
    ]
    with psycopg.connect(settings.migrate_database_url) as conn:
        sizes = conn.execute(
            "SELECT (SELECT count(*) FROM unit), (SELECT count(*) FROM score), (SELECT count(*) FROM quarantine),"
            " (SELECT count(*) FROM experiment)"
        ).fetchone()
        assert sizes is not None
        out += [
            f"Rows at capture: unit {sizes[0]:,}, score {sizes[1]:,}, quarantine {sizes[2]:,}, experiment {sizes[3]:,}.",
            "",
        ]
        run = conn.execute(
            "SELECT r.id, e.dataset_id FROM model_run r JOIN experiment e ON e.id = r.experiment_id"
            f" WHERE r.is_champion AND e.id = ({SERVED})"
        ).fetchone()
        assert run is not None, "seed and score the database first"
        middle = conn.execute(
            "SELECT uplift, unit_id FROM score WHERE run_id = %s AND split = 'test' ORDER BY uplift DESC, unit_id DESC OFFSET 20000 LIMIT 1",
            [run[0]],
        ).fetchone()
        assert middle is not None
        params = {"run": run[0], "dataset": run[1], "uplift": middle[0], "unit": middle[1]}
        conn.execute("ANALYZE")
        for title, why, sql, index in QUERIES:
            used = {k: v for k, v in params.items() if f"%({k})s" in sql}
            out += [f"## {title}", "", why, "", "```sql", sql, "```", "", "```", plan(conn, sql, used), "```", ""]
            if index:
                with conn.transaction(force_rollback=True):
                    conn.execute(f"DROP INDEX {index}")
                    out += [
                        f"The same query with `{index}` dropped (inside a transaction that is rolled back):",
                        "",
                        "```",
                        plan(conn, sql, used),
                        "```",
                        "",
                    ]
        conn.rollback()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
