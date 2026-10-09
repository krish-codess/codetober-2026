"""Capture EXPLAIN ANALYZE for the queries the serving API runs, into docs/evidence/explain-analyze.md.

    python scripts/explain.py            (uses WRAPPED_MIGRATE_DATABASE_URL; needs a published run)

Each index is justified by the query it serves. The one secondary index is also measured against
the same query with the index dropped inside a transaction that is rolled back.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from wrapped import config

OUT = Path(__file__).resolve().parent.parent / "docs" / "evidence" / "explain-analyze.md"

QUERIES = [
    (
        "Story lookup: `GET /v1/wrapped`",
        "Served by the primary keys `active_runs(year)` and `wrapped_payloads(run_id, user_id)`.",
        """SELECT p.payload, p.login, a.run_id, r.finished_at
           FROM active_runs a
           JOIN generation_runs r ON r.run_id = a.run_id
           JOIN wrapped_payloads p ON p.run_id = a.run_id
           WHERE a.year = %(year)s AND p.user_id = %(user)s""",
    ),
    (
        "Card-in-story check: `PUT /v1/wrapped/views/{card_type}`",
        "Served by the unique constraint `payload_cards(run_id, user_id, card_type)`.",
        """SELECT 1 FROM active_runs a JOIN payload_cards c ON c.run_id = a.run_id
           WHERE a.year = %(year)s AND c.user_id = %(user)s AND c.card_type = 'summary'""",
    ),
    (
        "Payload listing page: `GET /v1/admin/payloads`",
        "Keyset pagination on the primary key `wrapped_payloads(run_id, user_id)`: no offset, no sort.",
        """SELECT p.user_id, p.login, p.tier
           FROM wrapped_payloads p
           WHERE p.run_id = (SELECT run_id FROM active_runs WHERE year = %(year)s) AND p.user_id > %(user)s
           ORDER BY p.user_id LIMIT 51""",
    ),
    (
        "The same page as first written, joining to `active_runs` (kept as the reason for the rewrite)",
        "With the run id arriving through a join, the planner cannot use the key's order: it scans and sorts the run.",
        """SELECT p.user_id, p.login, p.tier
           FROM active_runs a JOIN wrapped_payloads p ON p.run_id = a.run_id
           WHERE a.year = %(year)s AND p.user_id > %(user)s ORDER BY p.user_id LIMIT 51""",
    ),
    (
        "Share lookup: `GET /v1/shares/{share_id}`",
        "Served by the primary key `shares(share_id)`.",
        "SELECT share_id, login, year, card, source_run_id FROM shares WHERE share_id = 'AAAAAAAAAAAAAAAAAAAAAA'",
    ),
    (
        "Share rate by card type: `GET /v1/admin/analytics/share-rate`",
        "Two small aggregates over `card_views` and `shares`. No index: every row of the year is read by design.",
        """SELECT t.card_type, coalesce(v.viewers, 0), coalesce(s.sharers, 0)
           FROM card_types t
           LEFT JOIN (SELECT card_type, count(*) AS viewers FROM card_views WHERE year = %(year)s GROUP BY card_type) v
               USING (card_type)
           LEFT JOIN (SELECT card_type, count(*) AS sharers FROM shares WHERE year = %(year)s GROUP BY card_type) s
               USING (card_type)
           WHERE t.shareable""",
    ),
]
DISTRIBUTION = """SELECT c.card_type, t.family, c.users
                  FROM (SELECT card_type, count(*) AS users FROM payload_cards WHERE run_id = %(run)s GROUP BY card_type) c
                  JOIN card_types t USING (card_type)"""
DISTRIBUTION_JOIN_FIRST = """SELECT c.card_type, t.family, count(*) AS users
                             FROM payload_cards c JOIN card_types t USING (card_type)
                             WHERE c.run_id = %(run)s GROUP BY c.card_type, t.family"""
INDEX = "payload_cards_run_type_idx"


def explain(conn: psycopg.Connection, sql: str, params: dict[str, object]) -> str:
    rows = conn.execute("EXPLAIN (ANALYZE, BUFFERS, TIMING, COSTS OFF) " + sql, params).fetchall()
    return "\n".join(r[0] for r in rows)


def main() -> None:
    settings = config.load()
    with psycopg.connect(settings.migrate_database_url) as conn:
        conn.execute("ANALYZE")
        run, users, cards = conn.execute(
            """SELECT a.run_id, (SELECT count(*) FROM wrapped_payloads p WHERE p.run_id = a.run_id),
                      (SELECT count(*) FROM payload_cards c WHERE c.run_id = a.run_id)
               FROM active_runs a WHERE a.year = %s""",
            [settings.year],
        ).fetchone()  # type: ignore[misc]
        runs_loaded = conn.execute("SELECT count(*) FROM generation_runs").fetchone()[0]  # type: ignore[index]
        user = conn.execute(
            "SELECT user_id FROM wrapped_payloads WHERE run_id = %s ORDER BY user_id OFFSET %s LIMIT 1",
            [run, users // 2],
        ).fetchone()[0]  # type: ignore[index]
        params = {"year": settings.year, "user": user, "run": run}
        version = conn.execute("SHOW server_version").fetchone()[0]  # type: ignore[index]

        out = [
            "# EXPLAIN ANALYZE for the serving queries",
            "",
            f"Captured by `scripts/explain.py` on PostgreSQL {version}. Active run: {users:,} payloads, {cards:,} "
            f"payload cards; {runs_loaded} run(s) loaded. Timings are from a laptop and matter only relative to each other.",
            "",
        ]
        for title, why, sql in QUERIES:
            out += [f"## {title}", "", why, "", "```", explain(conn, sql, params), "```", ""]

        out += [
            "## Superlative distribution: `GET /v1/admin/analytics/superlatives`",
            "",
            "Counts every card of the active run, so it reads the whole run by design. As shipped (aggregate, then join",
            "the catalogue), with no secondary index:",
            "",
            "```",
            explain(conn, DISTRIBUTION, params),
            "```",
            "",
            "As first written (join, then aggregate):",
            "",
            "```",
            explain(conn, DISTRIBUTION_JOIN_FIRST, params),
            "```",
            "",
            f"### The index that was removed: `{INDEX} (run_id, card_type)`",
            "",
            "The first schema had this index, added for this query. The same query with the index present, inside a",
            "transaction that is rolled back. The planner still chooses the sequential scan: one run is most of the",
            "table. The index was dropped in migration 0003.",
            "",
        ]
        conn.commit()
        conn.execute(f"CREATE INDEX {INDEX} ON payload_cards (run_id, card_type)")
        conn.execute("ANALYZE payload_cards")
        with_index = explain(conn, DISTRIBUTION, params)
        conn.rollback()
        out += ["```", with_index, "```", ""]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
