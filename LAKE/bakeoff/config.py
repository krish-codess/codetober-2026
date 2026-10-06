"""Environment configuration, structured logging and stage accounting."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import tomllib
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

# One id per CLI invocation or HTTP request, stamped on every log line it produces.
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

# Stated runtime budget per stage, in seconds, for the default three-month dataset (~9.5M rows).
# `timed` warns when a stage overruns; the report publishes budget next to actual.
STAGE_BUDGET_S = {"fetch": 300, "land": 120, "profile": 120, "ingest": 180, "materialize": 1500,
                  "bench": 6000, "columns": 600, "report": 30}


class ConfigError(ValueError):
    """A setting is missing or out of range. The message names the variable and the fix."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    published_dir: Path
    source: str
    months: tuple[str, ...]
    tlc_base_url: str
    synth_rows: int
    seed: int
    lab_rows: int
    cold_runs: int
    warmups: int
    warm_runs: int
    cell_budget_s: float
    threads: int
    port: int
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def integer(name: str, default: int, lo: int, hi: int) -> int:
            raw = env.get(name, str(default))
            try:
                value = int(raw)
            except ValueError:
                raise ConfigError(f"{name}={raw!r} is not an integer") from None
            if not lo <= value <= hi:
                raise ConfigError(f"{name}={value} must be between {lo} and {hi}")
            return value

        source = env.get("LAKE_SOURCE", "synthetic")
        if source not in ("tlc", "synthetic"):
            raise ConfigError(f"LAKE_SOURCE={source!r} must be 'tlc' or 'synthetic'")
        months = tuple(m.strip() for m in env.get("LAKE_MONTHS", "2024-01,2024-02,2024-03").split(","))
        for m in months:
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m):
                raise ConfigError(f"LAKE_MONTHS entry {m!r} must look like 2024-01")
        url = env.get("LAKE_TLC_BASE_URL", "https://d37ci6vzurychx.cloudfront.net/trip-data").rstrip("/")
        if not re.match(r"https?://", url):
            raise ConfigError("LAKE_TLC_BASE_URL must be an http(s) URL")
        try:
            budget = float(env.get("LAKE_CELL_BUDGET_S", "20"))
        except ValueError:
            raise ConfigError("LAKE_CELL_BUDGET_S must be a number of seconds") from None
        return cls(
            data_dir=Path(env.get("LAKE_DATA_DIR", "./data")).resolve(),
            published_dir=Path(env.get("LAKE_PUBLISHED_DIR", "./results")).resolve(),
            source=source,
            months=tuple(sorted(months)),
            tlc_base_url=url,
            synth_rows=integer("LAKE_SYNTH_ROWS", 300_000, 1_000, 500_000_000),
            seed=integer("LAKE_SEED", 8, 0, 2**31 - 1),
            lab_rows=integer("LAKE_LAB_ROWS", 1_000_000, 1_000, 100_000_000),
            cold_runs=integer("LAKE_COLD_RUNS", 3, 1, 50),
            warmups=integer("LAKE_WARMUPS", 1, 0, 50),
            warm_runs=integer("LAKE_WARM_RUNS", 5, 2, 200),
            cell_budget_s=budget,
            threads=integer("LAKE_THREADS", 0, 0, 1024) or (os.cpu_count() or 1),
            port=integer("LAKE_PORT", 8000, 1, 65535),
            log_level=env.get("LAKE_LOG_LEVEL", "INFO").upper(),
        )


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader: KEY=VALUE lines, real environment wins."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


def load_toml(name: str) -> dict[str, Any]:
    """Read a config file shipped inside the package (taxi.toml, pricing.toml)."""
    return tomllib.loads(resources.files("bakeoff").joinpath(name).read_text(encoding="utf-8"))


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "level": record.levelname,
               "cid": correlation_id.get(), "event": record.getMessage(), **getattr(record, "fields", {})}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


_logger = logging.getLogger("bakeoff")


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    _logger.handlers[:] = [handler]
    _logger.setLevel(level)
    _logger.propagate = False


def log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    _logger.log(level, event, extra={"fields": fields})


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid


def write_json(path: Path, obj: Any) -> None:
    """Atomic: a crash leaves the old file or the new one, never half of each."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


@contextmanager
def timed(s: Settings, stage: str) -> Iterator[dict[str, Any]]:
    """Time a stage, log its throughput, and record it in stages.json for the report.

    The body fills in `rows` / `bytes` on the yielded dict; throughput is measured, not assumed.
    """
    info: dict[str, Any] = {}
    log("stage.start", stage=stage)
    t0 = time.perf_counter()
    try:
        yield info
    except Exception:
        log("stage.failed", logging.ERROR, stage=stage, seconds=round(time.perf_counter() - t0, 3))
        raise
    seconds = time.perf_counter() - t0
    budget = STAGE_BUDGET_S.get(stage)
    entry = {"seconds": round(seconds, 3), "budget_s": budget, "at": datetime.now(UTC).isoformat(timespec="seconds"),
             **info}
    if "rows" in info and seconds > 0 and not info.get("skipped"):
        entry["rows_per_s"] = round(info["rows"] / seconds)
    if budget and seconds > budget:
        log("stage.over_budget", logging.WARNING, stage=stage, seconds=round(seconds, 1), budget_s=budget)
    if not info.get("skipped"):  # a skipped (already done) stage keeps the timing of the run that did the work
        stages = read_json(s.data_dir / "stages.json", {})
        stages[stage] = entry
        write_json(s.data_dir / "stages.json", stages)
    log("stage.done", stage=stage, **entry)
