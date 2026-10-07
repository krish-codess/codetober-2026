"""Environment configuration and structured logging. Nothing here has a secret default that works in production."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# One id per HTTP request or batch run; every log line carries it.
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

DEV_SECRET = "dev-only-secret-change-me"  # noqa: S105 - refused outside WRAPPED_ENV=dev, see Settings.check()


def _default_data_dir() -> str:
    # Keep multi-GB data out of the repo (and out of OneDrive on Windows).
    base = os.environ.get("LOCALAPPDATA")
    return str(Path(base) / "measure-wrapped") if base else str(Path.cwd() / "data")


@dataclass(frozen=True)
class Settings:
    env: str
    data_dir: Path
    year: int
    api_database_url: str
    batch_database_url: str
    migrate_database_url: str
    token_secret: str
    admin_token: str
    public_base_url: str
    duckdb_memory: str
    duckdb_threads: int
    k_anonymity: int
    log_level: str

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def bronze_dir(self) -> Path:
        return self.data_dir / "bronze"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    @property
    def warehouse_path(self) -> Path:
        return self.data_dir / "warehouse.duckdb"

    @property
    def payload_dir(self) -> Path:
        return self.data_dir / "payloads"

    @property
    def card_dir(self) -> Path:
        return self.data_dir / "cards"

    def check(self) -> None:
        """Fail fast on configuration that is only acceptable on a laptop."""
        if self.env != "dev" and DEV_SECRET in (self.token_secret, self.admin_token):
            raise SystemExit("WRAPPED_TOKEN_SECRET and WRAPPED_ADMIN_TOKEN must be set when WRAPPED_ENV is not 'dev'")
        if len(self.token_secret) < 16:
            raise SystemExit("WRAPPED_TOKEN_SECRET must be at least 16 characters")


def load() -> Settings:
    e = os.environ.get
    dev_db = "postgresql://wrapped@127.0.0.1:54329/wrapped"
    api_url = e("WRAPPED_DATABASE_URL", dev_db)
    s = Settings(
        env=e("WRAPPED_ENV", "dev"),
        data_dir=Path(e("WRAPPED_DATA_DIR") or _default_data_dir()),
        year=int(e("WRAPPED_YEAR", "2025")),
        api_database_url=api_url,
        batch_database_url=e("WRAPPED_BATCH_DATABASE_URL", api_url),
        migrate_database_url=e("WRAPPED_MIGRATE_DATABASE_URL", api_url),
        token_secret=e("WRAPPED_TOKEN_SECRET", DEV_SECRET),
        admin_token=e("WRAPPED_ADMIN_TOKEN", DEV_SECRET),
        public_base_url=e("WRAPPED_PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/"),
        duckdb_memory=e("WRAPPED_DUCKDB_MEMORY", "1GB"),
        duckdb_threads=int(e("WRAPPED_DUCKDB_THREADS", "4")),
        k_anonymity=int(e("WRAPPED_K_ANONYMITY", "10")),
        log_level=e("WRAPPED_LOG_LEVEL", "INFO"),
    )
    s.check()
    return s


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        out.update(getattr(record, "fields", {}))
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


def log(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields: object) -> None:
    """Structured log line: `log(logger, "ingested", files=3, rows=120)`."""
    logger.log(level, msg, extra={"fields": fields})
