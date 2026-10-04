"""All configuration comes from environment variables (see .env.example)."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GS_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    # Repo-relative defaults; containers set these to where the image copies them.
    migrations_dir: Path = Path(__file__).resolve().parents[2] / "migrations"
    reference_dir: Path = Path(__file__).resolve().parents[2] / "reference"
    # Pipeline writes as gs_pipeline, API reads as gs_api: least privilege per process.
    database_url: SecretStr = SecretStr("postgresql://gs_pipeline:pipeline@localhost:5433/goldstandard")
    api_database_url: SecretStr = SecretStr("postgresql://gs_api:api@localhost:5433/goldstandard")

    esi_base_url: str = "https://esi.evetech.net/latest"
    esi_user_agent: str = "goldstandard-cpi/0.1 (portfolio project)"
    http_timeout_s: float = 20.0
    http_max_retries: int = Field(default=5, ge=0, le=10)
    http_backoff_base_s: float = 0.5
    http_backoff_cap_s: float = 30.0
    esi_concurrency: int = Field(default=8, ge=1, le=32)

    # Synthetic world (fixed start so that scheduled ticks extend the same simulated history).
    synth_start: date = date(2025, 9, 1)
    synth_servers: int = Field(default=4, ge=1, le=8)
    synth_seed: int = 20251001
    synth_snapshots_per_day: int = Field(default=4, ge=1, le=24)

    log_level: str = "INFO"
    log_json: bool = True

    # Freshness SLO: data older than this is reported as stale by /health and the API.
    stale_after_hours: int = 48

    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def lake_dir(self) -> Path:
        return self.data_dir / "lake"


@lru_cache
def settings() -> Settings:
    return Settings()
