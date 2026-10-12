"""All configuration comes from the environment. See .env.example for what each variable means."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"GHOST_{name}", default)


def _sources(spec: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        name, sep, url = item.partition("=")
        if not sep or not name.isidentifier() or not url.startswith(("tcp://", "wss://", "ws://")):
            raise ValueError(
                f"GHOST_SOURCES entry {item!r} must look like name=tcp://host:port or name=wss://host/path"
            )
        out[name] = url
    return out


@dataclass(frozen=True)
class Settings:
    dsn: str = field(default_factory=lambda: _env("DSN", "postgresql://postgres@127.0.0.1:5432/ghost"))
    kafka: str = field(default_factory=lambda: _env("KAFKA", "127.0.0.1:9092"))
    sources: dict[str, str] = field(default_factory=lambda: _sources(_env("SOURCES", "")))
    aisstream_key: str = field(default_factory=lambda: os.environ.get("AISSTREAM_API_KEY", ""), repr=False)
    archive_dir: Path = field(default_factory=lambda: Path(_env("ARCHIVE_DIR", "data/raw")))
    metrics_port: int = field(default_factory=lambda: int(_env("METRICS_PORT", "0")))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    topic_positions: str = field(default_factory=lambda: _env("TOPIC_POSITIONS", "ais.positions"))
    topic_static: str = field(default_factory=lambda: _env("TOPIC_STATIC", "ais.static"))
    topic_dlq: str = field(default_factory=lambda: _env("TOPIC_DLQ", "ais.dlq"))
    partitions: int = field(default_factory=lambda: int(_env("PARTITIONS", "12")))
    retention_h: int = field(default_factory=lambda: int(_env("RETENTION_H", "72")))
    group: str = field(default_factory=lambda: _env("GROUP", "ghost-sink"))

    reconnect_base_s: float = field(default_factory=lambda: float(_env("RECONNECT_BASE_S", "1")))
    reconnect_cap_s: float = field(default_factory=lambda: float(_env("RECONNECT_CAP_S", "60")))
    idle_timeout_s: float = field(default_factory=lambda: float(_env("IDLE_TIMEOUT_S", "30")))
    fragment_timeout_s: float = field(default_factory=lambda: float(_env("FRAGMENT_TIMEOUT_S", "5")))

    # Detection. A moving vessel that is silent this long has a gap worth explaining; a moored one
    # may legitimately power down, so it gets a far longer leash.
    gap_moving_s: int = field(default_factory=lambda: int(_env("GAP_MOVING_S", "1800")))
    gap_stationary_s: int = field(default_factory=lambda: int(_env("GAP_STATIONARY_S", "21600")))
    max_speed_kn: float = field(default_factory=lambda: float(_env("MAX_SPEED_KN", "50")))
    neighbour_radius_m: float = field(default_factory=lambda: float(_env("NEIGHBOUR_RADIUS_M", "20000")))
    coverage_cell_deg: float = field(default_factory=lambda: float(_env("COVERAGE_CELL_DEG", "0.25")))
    coverage_min: float = field(default_factory=lambda: float(_env("COVERAGE_MIN", "0.8")))
    lookback_h: int = field(default_factory=lambda: int(_env("LOOKBACK_H", "72")))
    detect_interval_s: float = field(default_factory=lambda: float(_env("DETECT_INTERVAL_S", "60")))

    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(filter(None, _env("CORS_ORIGINS", "").split(",")))
    )
