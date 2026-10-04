"""Pipeline CLI. Every stage is independently runnable and idempotent:

python -m parse_app.pipeline feed        # build the immutable feed file (download or generate)
python -m parse_app.pipeline ingest      # validate + load it (once per file hash)
python -m parse_app.pipeline embed       # embed rows that have no embedding
python -m parse_app.pipeline simulate N  # oracle labels the top-N of the queue
python -m parse_app.pipeline train       # train, evaluate, gate, promote, rescore
python -m parse_app.pipeline seed        # all of the above, for a fresh database
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import Engine, text

from . import taxonomy
from .config import Settings, get_settings
from .corpus import CachedEmbedder, build_feed, load_taxonomy_paths, read_accepted
from .db import get_engine
from .embed import get_embedder
from .fetch import FetchError
from .ingest import gold_paths, ingest_file
from .log import correlation_id, event, setup_logging
from .store import embed_missing, queue_page, save_annotation
from .train import train

logger = logging.getLogger(__name__)
ROLES = {"ADMIN_TOKEN": "admin", "ANNOTATOR_TOKEN": "annotator", "VIEWER_TOKEN": "viewer"}


def seed_tokens(engine: Engine) -> int:
    n = 0
    with engine.begin() as conn:
        for var, role in ROLES.items():
            token = os.environ.get(var)
            if not token:
                continue
            n += conn.execute(
                text(
                    """INSERT INTO api_tokens (name, token_sha256, role) VALUES (:n, :h, :r)
                       ON CONFLICT (name) DO UPDATE SET token_sha256 = EXCLUDED.token_sha256, revoked_at = NULL"""
                ),
                {"n": f"seed-{role}", "h": hashlib.sha256(token.encode()).hexdigest(), "r": role},
            ).rowcount
    return n


def stage_feed(settings: Settings) -> tuple[Path, str]:
    """Returns (feed file, source actually used). Degrades to the synthetic generator - loudly -
    if the upstream download fails after its retries."""
    source = os.environ.get("SEED_SOURCE", "real")
    langs = [s for s in os.environ.get("SEED_TEST_LANGS", "en,es,de,fr,ja,zh,ru,ar,tr,hi,th,sw").split(",") if s]
    if source == "real":
        try:
            return build_feed(settings.data_dir, "real", langs), "real"
        except (FetchError, OSError) as e:
            event(logger, "seed_degraded_to_synthetic", logging.ERROR, error=str(e),
                  hint="M-ABSA could not be downloaded; seeding with generated data instead. "
                       "Fix connectivity, drop the database volume and re-run to get the real corpus.")  # fmt: skip
    return build_feed(settings.data_dir, "synthetic", ["en", "es", "de", "sw"]), "synthetic"


def stage_embed(engine: Engine, settings: Settings) -> int:
    embedder = CachedEmbedder(get_embedder(settings), settings.data_dir / "cache" / f"emb-{settings.embed_backend}.npz")
    t = time.perf_counter()
    n = embed_missing(engine, embedder, settings.embed_model_name)
    event(logger, "embed_done", rows=n, seconds=round(time.perf_counter() - t, 1))
    return n


def simulate(engine: Engine, n: int, seed: int = 0) -> int:
    """Stand-in for a human annotator: answers with the reference label hidden in the raw payload.
    Takes the top of the active-learning queue; before any model exists it samples at random."""
    with engine.begin() as conn:
        known = taxonomy.active_paths(conn)
        ids = [r.id for r in queue_page(conn, limit=n)]
        if not ids:
            candidates = (
                conn.execute(
                    text(
                        """SELECT f.id FROM feedback f LEFT JOIN annotations a ON a.feedback_id = f.id
                       WHERE f.split = 'pool' AND f.duplicate_of IS NULL AND a.feedback_id IS NULL ORDER BY f.id"""
                    )
                )
                .scalars()
                .all()
            )
            rng = np.random.default_rng(seed)
            ids = [int(i) for i in rng.choice(candidates, size=min(n, len(candidates)), replace=False)]
        rows = conn.execute(
            text(
                """SELECT f.id, r.payload FROM feedback f
                   JOIN raw_feedback r ON r.batch_id = f.batch_id AND r.line_no = f.line_no WHERE f.id = ANY(:ids)"""
            ),
            {"ids": ids},
        ).all()
        done = 0
        for row in rows:
            gold = json.loads(bytes(row.payload)).get("gold")
            if not gold:
                continue
            nodes, _ = gold_paths((gold["domain"], tuple(gold["categories"])), known)
            save_annotation(conn, row.id, sorted(nodes), annotator="oracle", source="simulated")
            done += 1
    return done


def seed(engine: Engine, settings: Settings) -> dict[str, Any]:
    out: dict[str, Any] = {"tokens": seed_tokens(engine)}
    feed, source = stage_feed(settings)
    out["source"] = source
    with engine.begin() as conn:
        out["taxonomy"] = taxonomy.seed(
            conn, load_taxonomy_paths(source, read_accepted(feed) if source != "real" else [])
        )
    out["ingest"] = ingest_file(engine, feed, default_source="feed")
    out["embedded"] = stage_embed(engine, settings)

    with engine.connect() as conn:
        has_model = conn.execute(text("SELECT 1 FROM model_versions LIMIT 1")).first() is not None
    if not has_model:
        # Bootstrap the loop: a random first batch, then active-learning rounds, so the first
        # screen a user sees already has a model, a queue and a short efficiency curve.
        target = int(os.environ.get("SEED_LABELS", "300"))
        rounds = []
        labelled = simulate(engine, min(100, target))
        while True:
            result = train(engine, settings)
            rounds.append({"n_labeled": result["n_labeled"], "hf1": round(result["hf1"], 4)})
            if labelled >= target:
                break
            labelled += simulate(engine, min(50, target - labelled))
        out["bootstrap"] = rounds
    return out


def main(argv: list[str]) -> int:
    settings = get_settings()
    setup_logging(settings.log_level)
    correlation_id.set(f"cli-{argv[0] if argv else 'help'}-{int(time.time())}")
    if not argv:
        print(__doc__)
        return 2
    cmd, engine = argv[0], get_engine()
    if cmd == "feed":
        result: Any = {"feed": str(stage_feed(settings)[0])}
    elif cmd == "ingest":
        result = ingest_file(engine, Path(argv[1]) if len(argv) > 1 else stage_feed(settings)[0])
    elif cmd == "embed":
        result = {"embedded": stage_embed(engine, settings)}
    elif cmd == "simulate":
        result = {"labelled": simulate(engine, int(argv[1]) if len(argv) > 1 else 50)}
    elif cmd == "train":
        result = train(engine, settings)
    elif cmd == "tokens":
        result = {"tokens": seed_tokens(engine)}
    elif cmd == "seed":
        result = seed(engine, settings)
    else:
        print(__doc__)
        return 2
    event(logger, f"{cmd}_result", result=result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
