"""Every setting comes from the environment (see .env.example). Nothing here is a secret."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    # The repository lives in a synced folder on the author's machine; multi-GB data must not.
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "render-dots"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    site_dir: Path
    source: str  # glove-twitter-50 | synth | auto
    glove_urls: tuple[str, ...]
    synth_rows: int
    seed: int
    landmarks: int
    tile_capacity: int
    variants: tuple[str, ...]
    eval_queries: int
    cors_origins: tuple[str, ...]

    def raw(self, source: str) -> Path:
        return self.data_dir / "raw" / source

    def work(self, source: str) -> Path:
        return self.data_dir / "work" / source


def settings() -> Settings:
    env = os.environ.get
    data_dir = Path(env("DOTS_DATA_DIR") or _default_data_dir())
    return Settings(
        data_dir=data_dir,
        site_dir=Path(env("DOTS_SITE_DIR") or data_dir / "site"),
        source=env("DOTS_SOURCE", "auto"),
        glove_urls=tuple(
            env(
                "DOTS_GLOVE_URLS",
                "https://huggingface.co/stanfordnlp/glove/resolve/main/glove.twitter.27B.zip,"
                "https://downloads.cs.stanford.edu/nlp/data/glove.twitter.27B.zip",
            ).split(",")
        ),
        synth_rows=int(env("DOTS_SYNTH_ROWS", "200000")),
        seed=int(env("DOTS_SEED", "15")),
        landmarks=int(env("DOTS_LANDMARKS", "100000")),
        tile_capacity=int(env("DOTS_TILE_CAPACITY", "32768")),
        variants=tuple(env("DOTS_VARIANTS", "pca,umap-n15-d0.1,umap-n50-d0.5").split(",")),
        eval_queries=int(env("DOTS_EVAL_QUERIES", "4000")),
        cors_origins=tuple(o for o in env("DOTS_CORS_ORIGINS", "").split(",") if o),
    )


# --- structured logs -------------------------------------------------------------------------

RUN_ID = os.environ.get("DOTS_RUN_ID") or uuid.uuid4().hex[:12]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
            "run_id": RUN_ID,
        }
        out.update(getattr(record, "fields", {}))
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def get_logger() -> logging.Logger:
    log = logging.getLogger("dots")
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        log.addHandler(handler)
        log.setLevel(os.environ.get("DOTS_LOG_LEVEL", "INFO").upper())
        log.propagate = False
    return log


def event(msg: str, level: int = logging.INFO, **fields: object) -> None:
    """One structured log line: `event("tiles written", variant="pca", tiles=57)`."""
    get_logger().log(level, msg, extra={"fields": fields})


def write_json(path: Path, obj: object) -> None:
    """Atomic write, so a killed stage never leaves a half-written file that looks complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
