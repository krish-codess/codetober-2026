"""All configuration comes from the environment. Nothing here has a secret default."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Pinned upstream revisions: raw inputs are immutable, so any derived output is reproducible.
MABSA_REPO = "Multilingual-NLP/M-ABSA"
MABSA_REVISION = "521ab96c3fc0acb4f844205d0e05382734fc89f2"
EMBED_REPO = "Xenova/multilingual-e5-small"
EMBED_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
EMBED_FILE = "onnx/model_quantized.onnx"
EMBED_DIM = 384


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    embed_backend: str
    log_level: str
    cors_origins: tuple[str, ...]
    retrain_interval_s: int
    retrain_min_new_labels: int
    gate_max_drop: float

    @property
    def embed_model_name(self) -> str:
        if self.embed_backend == "hash":
            return "hash-ngram-384"
        return f"{EMBED_REPO}@{EMBED_REVISION[:8]}/{EMBED_FILE}"


@lru_cache
def get_settings() -> Settings:
    env = os.environ
    backend = env.get("EMBED_BACKEND", "onnx")
    if backend not in ("onnx", "hash"):
        raise ValueError(f"EMBED_BACKEND must be 'onnx' or 'hash', got {backend!r}")
    return Settings(
        database_url=env["DATABASE_URL"],  # required: fail loudly at startup, not at first query
        data_dir=Path(env.get("DATA_DIR", "data")),
        embed_backend=backend,
        log_level=env.get("LOG_LEVEL", "INFO"),
        cors_origins=tuple(o for o in env.get("CORS_ORIGINS", "").split(",") if o),
        retrain_interval_s=int(env.get("RETRAIN_INTERVAL_S", "900")),
        retrain_min_new_labels=int(env.get("RETRAIN_MIN_NEW_LABELS", "25")),
        gate_max_drop=float(env.get("GATE_MAX_DROP", "0.02")),
    )
