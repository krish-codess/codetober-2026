"""Synthetic GH Archive feed: same file naming, same JSON shape, same skew, and the defects a year of it has.

A real year of GH Archive is ~1.3 TB compressed, so the demonstration year is generated. The shape is
calibrated against real hours (docs/data-profile.md): event-type mix, share of events from bots, the
one-event-per-actor majority, the PushEvent payload losing `size` in October 2025, a new event type
appearing late in the year. The defects are injected at known rates and counted in a ledger
(`generator_truth.json`) so tests can reconcile what ingest quarantined against what was planted.

Deterministic for a given (seed, users, year): numpy draws per user, DuckDB sorts on disk, Python writes.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
from pathlib import Path

import duckdb
import numpy as np

from wrapped.config import log

logger = logging.getLogger(__name__)

# Event-type mix measured on real hours, humans and bots together (docs/data-profile.md).
EVENT_TYPES = [
    "PushEvent", "CreateEvent", "PullRequestEvent", "IssueCommentEvent", "WatchEvent", "PullRequestReviewEvent",
    "DeleteEvent", "PullRequestReviewCommentEvent", "IssuesEvent", "ForkEvent", "ReleaseEvent", "MemberEvent",
    "PublicEvent", "GollumEvent", "CommitCommentEvent",
]  # fmt: skip
BASE_MIX = np.array([52, 11, 8, 6, 9, 4, 2.5, 2.5, 3, 2.5, 0.6, 0.4, 0.3, 0.15, 0.05])
# Personas tilt the base mix so that users genuinely differ in what they are unusual at.
PERSONAS: dict[str, dict[str, float]] = {
    "generalist": {},
    "builder": {"PushEvent": 3, "CreateEvent": 2, "ReleaseEvent": 4},
    "reviewer": {"PullRequestReviewEvent": 14, "PullRequestReviewCommentEvent": 10, "PullRequestEvent": 3},
    "stargazer": {"WatchEvent": 12, "ForkEvent": 5, "PushEvent": 0.2},
    "triager": {"IssuesEvent": 10, "IssueCommentEvent": 9, "PushEvent": 0.4},
    "contributor": {"PullRequestEvent": 8, "ForkEvent": 3, "IssueCommentEvent": 3},
}
PERSONA_WEIGHTS = [0.34, 0.26, 0.08, 0.14, 0.08, 0.10]
OWN_REPO_TYPES = {
    "PushEvent",
    "CreateEvent",
    "DeleteEvent",
    "ReleaseEvent",
    "MemberEvent",
    "PublicEvent",
    "GollumEvent",
}
ACTIONS = {
    "PullRequestEvent": ["opened", "closed", "opened", "closed", "reopened"],
    "IssuesEvent": ["opened", "opened", "closed", "reopened"],
    "IssueCommentEvent": ["created"],
    "WatchEvent": ["started"],
    "PullRequestReviewEvent": ["created"],
    "PullRequestReviewCommentEvent": ["created"],
    "ReleaseEvent": ["published"],
    "MemberEvent": ["added"],
}
REF_TYPES = ["branch", "branch", "branch", "repository", "tag"]

# (login, account id, share of human event volume). The four labelled bots keep their well-known public ids;
# the last two are invented accounts standing in for automation that does not label itself.
BOTS = [
    ("github-actions[bot]", 41898282, 0.150),
    ("dependabot[bot]", 49699333, 0.025),
    ("renovate[bot]", 29139614, 0.015),
    ("pull[bot]", 39814207, 0.010),
    ("mirror-sync-runner", 990000001, 0.030),
    ("nightly-ci-user", 990000002, 0.012),
]

# Defect rates per emitted event.
P_LATE = 0.004  # delivered in a later hourly file than the hour it happened in
P_DUPLICATE = 0.002  # same event id delivered twice
P_TS_VARIANT = 0.005  # valid timestamp, different spelling (+00:00, millis, non-UTC offset)
P_MISSING_ACTOR = 0.0005
P_MISSING_ID = 0.0002
P_BAD_TIMESTAMP = 0.0002
P_FUTURE_TIMESTAMP = 0.0001
P_MALFORMED = 0.0002  # line truncated mid-object
P_RENAMED_USER = 0.005  # user changes login mid-year; the id is the identity

ADJECTIVES = "quiet brisk amber lunar rapid mellow vivid stark gentle bold lucid nimble dusty frosty sunny wry".split()
NOUNS = (
    "otter falcon maple quartz ember harbor pixel comet willow badger cobalt meadow raven summit tundra lynx".split()
)
PROJECTS = "api core tools site notes engine dotfiles lab kit cli utils demo".split()


def _login(user_id: int) -> str:
    return f"{ADJECTIVES[user_id % 16]}-{NOUNS[(user_id // 16) % 16]}-{user_id % 9973}"


def _human_events(rng: np.random.Generator, user_ids: np.ndarray, year: int) -> dict[str, np.ndarray]:
    """Draw every event for a block of users. Returns flat per-event arrays."""
    n_users = len(user_ids)
    days_in_year = (dt.date(year + 1, 1, 1) - dt.date(year, 1, 1)).days
    jan1_weekday = dt.date(year, 1, 1).weekday()

    # Heavy tail: the median user does a handful of things, a few do tens of thousands.
    n_events = np.minimum(np.ceil(rng.lognormal(1.7, 1.75, n_users)), 40_000).astype(np.int64)

    persona = rng.choice(len(PERSONAS), n_users, p=PERSONA_WEIGHTS)
    tilt = np.ones((len(PERSONAS), len(EVENT_TYPES)))
    for i, boosts in enumerate(PERSONAS.values()):
        for name, factor in boosts.items():
            tilt[i, EVENT_TYPES.index(name)] = factor
    mix = BASE_MIX * tilt[persona] * rng.gamma(2.0, 0.5, (n_users, len(EVENT_TYPES)))
    mix_cdf = np.cumsum(mix / mix.sum(axis=1, keepdims=True), axis=1)

    tz_offset = rng.choice(np.arange(-8, 10), n_users)
    peak_local = np.clip(rng.normal(15, 4, n_users), 0, 23.99)
    weekend_keep = rng.beta(1.2, 2.0, n_users) * 1.4  # probability a weekend event stays on the weekend
    first_day = np.where(rng.random(n_users) < 0.5, 0, rng.integers(0, days_in_year - 20, n_users))
    window = np.maximum(1, ((days_in_year - first_day) * rng.beta(2.5, 1.2, n_users)).astype(np.int64))
    streak_len = np.minimum(rng.geometric(0.06, n_users), window)
    streak_start = first_day + (rng.random(n_users) * (window - streak_len + 1)).astype(np.int64)
    streak_share = rng.beta(1.5, 3.0, n_users)
    own_repos = np.minimum(rng.geometric(0.45, n_users), len(PROJECTS))

    u = np.repeat(np.arange(n_users), n_events)  # index of the owning user, per event
    n = len(u)
    in_streak = rng.random(n) < streak_share[u]
    day = np.where(
        in_streak,
        streak_start[u] + (rng.random(n) * streak_len[u]).astype(np.int64),
        first_day[u] + (rng.random(n) * window[u]).astype(np.int64),
    )
    weekday = (day + jan1_weekday) % 7
    move = (weekday >= 5) & (rng.random(n) > weekend_keep[u])
    day = np.minimum(np.where(move, day + (7 - weekday), day), days_in_year - 1)

    local_hour = np.where(rng.random(n) < 0.15, rng.random(n) * 24, rng.normal(peak_local[u], 2.5))
    second_of_day = (((local_hour - tz_offset[u]) % 24) * 3600).astype(np.int64)

    type_idx = (rng.random(n)[:, None] > mix_cdf[u]).sum(axis=1).clip(max=len(EVENT_TYPES) - 1)
    own = np.isin(type_idx, [EVENT_TYPES.index(t) for t in OWN_REPO_TYPES])
    # Own repos: favourite first. Shared repos: Zipf over a common pool, so popular projects exist.
    own_slot = np.minimum(rng.geometric(0.6, n) - 1, own_repos[u] - 1)
    shared_repo = np.minimum(rng.zipf(1.35, n), 50_000)
    repo_id = np.where(own, user_ids[u] * 16 + own_slot, 900_000_000 + shared_repo)

    return {
        "actor_id": user_ids[u],
        "ts": day * 86400 + second_of_day,
        "type_idx": type_idx.astype(np.int16),
        "repo_id": repo_id,
        "own": own,
        "pick": rng.integers(0, 1 << 30, n),  # per-event entropy for action/ref_type and defect draws
    }


def _bot_events(rng: np.random.Generator, n_human: int, year: int) -> dict[str, np.ndarray]:
    days_in_year = (dt.date(year + 1, 1, 1) - dt.date(year, 1, 1)).days
    parts: dict[str, list[np.ndarray]] = {k: [] for k in ("actor_id", "ts", "type_idx", "repo_id", "own", "pick")}
    for _name, bot_id, share in BOTS:
        n = int(n_human * share)
        parts["actor_id"].append(np.full(n, bot_id))
        parts["ts"].append(rng.integers(0, days_in_year * 86400, n))
        parts["type_idx"].append(rng.choice([0, 0, 0, 0, 1, 2, 6], n).astype(np.int16))
        parts["repo_id"].append(900_000_000 + np.minimum(rng.zipf(1.2, n), 50_000))
        parts["own"].append(np.zeros(n, dtype=bool))
        parts["pick"].append(rng.integers(0, 1 << 30, n))
    return {k: np.concatenate(v) for k, v in parts.items()}


_RENDER_SQL = """
CREATE TABLE rendered AS
WITH base AS (
    SELECT *,
           TIMESTAMP '{year}-01-01' + to_seconds(ts) AS happened_at,
           pick / 1073741824.0 AS r,           -- uniform [0,1): which defect, if any
           (pick * 2654435761) % 1000003 AS h  -- independent-ish integer for small choices
    FROM events
), flagged AS (
    SELECT *,
           CASE
               WHEN r < {p_missing_actor} THEN 'missing_actor'
               WHEN r < {p_missing_actor} + {p_missing_id} THEN 'missing_event_id'
               WHEN r < {p_missing_actor} + {p_missing_id} + {p_bad_ts} THEN 'unparseable_timestamp'
               WHEN r < {p_missing_actor} + {p_missing_id} + {p_bad_ts} + {p_future} THEN 'timestamp_in_future'
               WHEN r < {p_missing_actor} + {p_missing_id} + {p_bad_ts} + {p_future} + {p_malformed} THEN 'malformed_json'
           END AS defect,
           r > 1 - {p_late} AS is_late,
           r BETWEEN 0.5 AND 0.5 + {p_dup} AS is_dup,
           r BETWEEN 0.6 AND 0.6 + {p_ts_variant} AS ts_variant
    FROM base
), shaped AS (
    SELECT
        defect, is_dup,
        -- Late events land in a later hourly file: usually hours later, occasionally weeks.
        date_trunc('hour', happened_at
            + CASE WHEN is_late THEN to_hours(1 + (h % CASE WHEN h % 10 = 0 THEN 600 ELSE 48 END)::INT)
                   ELSE INTERVAL 0 HOUR END) AS file_hour,
        happened_at,
        CASE WHEN defect = 'missing_event_id' THEN NULL ELSE (40000000000 + event_seq)::VARCHAR END AS id,
        t.name AS type,
        CASE WHEN defect = 'missing_actor' THEN NULL ELSE {{
            'id': e.actor_id,
            -- A renamed user keeps the id and changes login from July onwards.
            'login': CASE WHEN u.renamed AND happened_at >= TIMESTAMP '{year}-07-01' THEN u.login || '-dev' ELSE u.login END
        }} END AS actor,
        {{'id': e.repo_id,
          'name': CASE WHEN e.own THEN u.login || '/' || list_extract({projects}, (e.repo_id % 16)::INT + 1)
                       ELSE 'org-' || (e.repo_id % 977)::VARCHAR || '/' || list_extract({projects}, (e.repo_id % 12)::INT + 1)
                            || '-' || (e.repo_id - 900000000)::VARCHAR END
        }} AS repo,
        CASE
            WHEN t.name = 'PushEvent' AND happened_at < TIMESTAMP '{year}-10-07'
                THEN json_object('ref', 'refs/heads/main', 'size', 1 + h % 4, 'distinct_size', 1 + h % 4)
            WHEN t.name = 'PushEvent' THEN json_object('ref', 'refs/heads/main')  -- upstream dropped the commit counts
            WHEN t.name IN ('CreateEvent', 'DeleteEvent') THEN json_object('ref_type', list_extract({ref_types}, (h % 5)::INT + 1))
            WHEN t.actions IS NOT NULL THEN json_object('action', list_extract(t.actions, (h % len(t.actions))::INT + 1))
            ELSE json_object()
        END AS payload,
        CASE
            WHEN defect = 'unparseable_timestamp' THEN CASE WHEN h % 2 = 0 THEN '' ELSE 'not-a-date' END
            WHEN defect = 'timestamp_in_future' THEN '2099-01-01T00:00:00Z'
            WHEN ts_variant AND h % 3 = 0 THEN strftime(happened_at, '%Y-%m-%dT%H:%M:%S+00:00')
            WHEN ts_variant AND h % 3 = 1 THEN strftime(happened_at, '%Y-%m-%dT%H:%M:%S.000Z')
            WHEN ts_variant THEN strftime(happened_at - INTERVAL 7 HOUR, '%Y-%m-%dT%H:%M:%S-07:00')
            ELSE strftime(happened_at, '%Y-%m-%dT%H:%M:%SZ')
        END AS created_at
    FROM flagged e
    JOIN actors u USING (actor_id)
    JOIN types t USING (type_idx)
)
SELECT file_hour, happened_at, defect, 0 AS copy,
       to_json({{'id': id, 'type': type, 'actor': actor, 'repo': repo, 'payload': payload, 'public': true,
                'created_at': created_at}})::VARCHAR AS line
FROM shaped
UNION ALL  -- redelivery: same event again, in the same file or the next hour's
SELECT file_hour + to_hours((hash(id) % 2)::INT), happened_at, 'duplicate', 1,
       to_json({{'id': id, 'type': type, 'actor': actor, 'repo': repo, 'payload': payload, 'public': true,
                'created_at': created_at}})::VARCHAR
FROM shaped WHERE is_dup AND defect IS NULL
"""


def generate(out_dir: Path, users: int, year: int, seed: int, work_dir: Path, memory: str = "1GB") -> dict[str, object]:
    """Write hourly `YYYY-MM-DD-H.json.gz` files into out_dir. Returns the truth ledger."""
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    scratch = work_dir / f"generate-{seed}-{users}.duckdb"
    scratch.unlink(missing_ok=True)
    rng = np.random.default_rng(seed)
    con = duckdb.connect(str(scratch), config={"memory_limit": memory, "temp_directory": str(work_dir / "tmp")})
    try:
        con.execute(
            "CREATE TABLE events (actor_id BIGINT, ts BIGINT, type_idx SMALLINT, repo_id BIGINT, own BOOLEAN, pick BIGINT)"
        )
        # GitHub-like sparse ids. Drawn up front so user blocks stay independent of block size.
        all_ids = 1_000_000 + np.cumsum(rng.integers(1, 400, users))
        n_human = 0
        for start in range(0, users, 20_000):
            block = _human_events(rng, all_ids[start : start + 20_000], year)  # noqa: F841 - scanned by DuckDB below
            con.execute("INSERT INTO events SELECT actor_id, ts, type_idx, repo_id, own, pick FROM block")
            n_human += len(block["ts"])
        bots = _bot_events(rng, n_human, year)  # noqa: F841
        con.execute("INSERT INTO events SELECT actor_id, ts, type_idx, repo_id, own, pick FROM bots")

        # A few events that happened in the last hours of the previous year arrive in this year's first files.
        con.execute(
            "INSERT INTO events SELECT actor_id, -(pick % 7200) - 1, type_idx, repo_id, own, pick FROM events "
            "WHERE pick % 20000 = 7 AND ts > 0"
        )
        con.execute("ALTER TABLE events ADD COLUMN event_seq BIGINT")
        con.execute("UPDATE events SET event_seq = rowid")

        renamed = set(rng.choice(all_ids, max(1, int(users * P_RENAMED_USER)), replace=False).tolist())
        actor_rows = [(int(i), _login(int(i)), int(i) in renamed) for i in all_ids] + [
            (b, name, False) for name, b, _ in BOTS
        ]
        con.execute("CREATE TABLE actors (actor_id BIGINT PRIMARY KEY, login VARCHAR, renamed BOOLEAN)")
        _bulk_actors(con, actor_rows)
        con.execute("CREATE TABLE types (type_idx SMALLINT, name VARCHAR, actions VARCHAR[])")
        con.executemany(
            "INSERT INTO types VALUES (?, ?, ?)", [(i, t, ACTIONS.get(t)) for i, t in enumerate(EVENT_TYPES)]
        )
        # Schema drift: a type that does not exist for most of the year. Re-label some late comments.
        con.execute("INSERT INTO types VALUES (?, 'DiscussionEvent', ['created'])", [len(EVENT_TYPES)])
        con.execute(
            f"UPDATE events SET type_idx = {len(EVENT_TYPES)} WHERE type_idx = 3 AND pick % 40 = 0 AND ts >= 273 * 86400"
        )

        con.execute(
            _RENDER_SQL.format(
                year=year, projects=PROJECTS + PROJECTS[:4], ref_types=REF_TYPES,
                p_missing_actor=P_MISSING_ACTOR, p_missing_id=P_MISSING_ID, p_bad_ts=P_BAD_TIMESTAMP,
                p_future=P_FUTURE_TIMESTAMP, p_malformed=P_MALFORMED, p_late=P_LATE, p_dup=P_DUPLICATE,
                p_ts_variant=P_TS_VARIANT,
            )
        )  # fmt: skip

        truth = _write_files(con, out_dir)
        truth.update(seed=seed, users=users, year=year, bot_accounts=len(BOTS), renamed_users=len(renamed))
        (out_dir / "generator_truth.json").write_text(json.dumps(truth, indent=2))
        log(logger, "generated", **{k: v for k, v in truth.items() if k != "defects"})
        return truth
    finally:
        con.close()
        scratch.unlink(missing_ok=True)


def _bulk_actors(con: duckdb.DuckDBPyConnection, rows: list[tuple[int, str, bool]]) -> None:
    cols = {  # noqa: F841 - scanned by DuckDB
        "actor_id": np.array([r[0] for r in rows]),
        "login": np.array([r[1] for r in rows], dtype=object),
        "renamed": np.array([r[2] for r in rows]),
    }
    con.execute("INSERT INTO actors SELECT * FROM cols")


def _write_files(con: duckdb.DuckDBPyConnection, out_dir: Path) -> dict[str, object]:
    defects: dict[str, int] = {}
    files = lines = 0
    current: dt.datetime | None = None
    fh: gzip.GzipFile | None = None
    cur = con.execute("SELECT file_hour, defect, line FROM rendered ORDER BY file_hour, happened_at, copy")
    while rows := cur.fetchmany(50_000):
        for file_hour, defect, line in rows:
            if file_hour != current:
                if fh:
                    fh.close()
                current = file_hour
                # GH Archive does not zero-pad the hour.
                fh = gzip.open(out_dir / f"{file_hour:%Y-%m-%d}-{file_hour.hour}.json.gz", "wb", compresslevel=3)
                files += 1
            if defect:
                defects[defect] = defects.get(defect, 0) + 1
                if defect == "malformed_json":
                    line = line[: len(line) // 2]  # an upload cut off mid-object
            assert fh is not None
            fh.write(line.encode() + b"\n")
            lines += 1
    if fh:
        fh.close()
    late = con.execute(
        "SELECT count(*) FROM rendered WHERE copy = 0 AND file_hour > date_trunc('hour', happened_at)"
    ).fetchone()
    assert late is not None
    return {"files": files, "lines": lines, "defects": defects, "late_arrivals": late[0]}
