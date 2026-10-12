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

DEV_TOKEN = "dev-only-token-change-me"  # noqa: S105 - refused outside UPLIFT_ENV=dev, see Settings.check()
HILLSTROM_URL = (
    "http://www.minethatdata.com/Kevin_Hillstrom_MineThatData_E-MailAnalytics_DataMiningChallenge_2008.03.20.csv"
)
# The published file. A download that does not hash to this is not the dataset the results describe.
HILLSTROM_SHA256 = "0e5893329d8b93cefecc571777672028290ab69865718020c78c7284f291aece"


def _default_data_dir() -> str:
    # Keep raw files and model artifacts out of the repo (and out of OneDrive on Windows).
    base = os.environ.get("LOCALAPPDATA")
    return str(Path(base) / "infer-uplift") if base else str(Path.cwd() / "data")


@dataclass(frozen=True)
class Settings:
    env: str
    data_dir: Path
    api_database_url: str
    batch_database_url: str
    migrate_database_url: str
    read_token: str
    admin_token: str
    hillstrom_url: str
    log_level: str

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    def check(self) -> None:
        """Fail fast on configuration that is only acceptable on a laptop."""
        if self.env != "dev" and DEV_TOKEN in (self.read_token, self.admin_token):
            raise SystemExit("UPLIFT_READ_TOKEN and UPLIFT_ADMIN_TOKEN must be set when UPLIFT_ENV is not 'dev'")
        if self.read_token == self.admin_token and self.env != "dev":
            raise SystemExit("UPLIFT_READ_TOKEN and UPLIFT_ADMIN_TOKEN must differ")


def load() -> Settings:
    e = os.environ.get
    dev_db = "postgresql://uplift@127.0.0.1:54331/uplift"
    api_url = e("UPLIFT_DATABASE_URL", dev_db)
    s = Settings(
        env=e("UPLIFT_ENV", "dev"),
        data_dir=Path(e("UPLIFT_DATA_DIR") or _default_data_dir()),
        api_database_url=api_url,
        batch_database_url=e("UPLIFT_BATCH_DATABASE_URL", api_url),
        migrate_database_url=e("UPLIFT_MIGRATE_DATABASE_URL", api_url),
        read_token=e("UPLIFT_READ_TOKEN", DEV_TOKEN),
        admin_token=e("UPLIFT_ADMIN_TOKEN", DEV_TOKEN),
        hillstrom_url=e("UPLIFT_HILLSTROM_URL", HILLSTROM_URL),
        log_level=e("UPLIFT_LOG_LEVEL", "INFO"),
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


def log(logger: logging.Logger, msg: str, /, **fields: object) -> None:
    """Structured log line: `log(logger, "ingested", rows=120)`."""
    logger.info(msg, extra={"fields": fields})


def warn(logger: logging.Logger, msg: str, /, **fields: object) -> None:
    logger.warning(msg, extra={"fields": fields})
