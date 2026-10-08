"""All configuration comes from the environment (see .env.example)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _default_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "vision-squeeze" if local else Path.home() / ".cache" / "vision-squeeze"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    packages_dir: Path
    raw_dir: Path
    data_dir: Path
    web_dir: Path
    device_token: str
    admin_token: str
    max_body: int
    log_level: str


def load() -> Settings:
    env = os.environ
    return Settings(
        db_path=Path(env.get("SQUEEZE_DB", "squeeze.db")),
        packages_dir=Path(env.get("SQUEEZE_PACKAGES_DIR", "release")),
        raw_dir=Path(env.get("SQUEEZE_RAW_DIR", "results/raw")),
        data_dir=Path(env.get("SQUEEZE_DATA_DIR") or _default_data_dir()),
        web_dir=Path(env.get("SQUEEZE_WEB_DIR", "web/dist")),
        device_token=env.get("SQUEEZE_DEVICE_TOKEN", ""),
        admin_token=env.get("SQUEEZE_ADMIN_TOKEN", ""),
        max_body=int(env.get("SQUEEZE_MAX_BODY", "262144")),
        log_level=env.get("SQUEEZE_LOG_LEVEL", "INFO"),
    )


def targets() -> dict[str, dict[str, Any]]:
    """The hardware targets, from targets.json next to the package (override: SQUEEZE_TARGETS)."""
    path = os.environ.get("SQUEEZE_TARGETS") or Path(__file__).resolve().parent.parent / "targets.json"
    return {t["name"]: t for t in json.loads(Path(path).read_text())}
