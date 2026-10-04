"""Structured JSON logs with a correlation id, plus in-process metrics in Prometheus text format."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import defaultdict
from contextvars import ContextVar
from typing import Any

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        out.update(getattr(record, "fields", {}))
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})


# ponytail: per-process counters; with >1 API replica scrape each replica (or switch to prometheus_client
# multiprocess mode). One uvicorn process per container today.
class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._counters[(name, tuple(sorted(labels.items())))] += value

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        self.inc(f"{name}_seconds_sum", seconds, **labels)
        self.inc(f"{name}_seconds_count", 1.0, **labels)

    def render(self) -> str:
        with self._lock:
            items = sorted(self._counters.items())
        lines = []
        for (name, labels), value in items:
            label_s = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{label_s}}} {value:g}" if label_s else f"{name} {value:g}")
        return "\n".join(lines) + "\n"


metrics = Metrics()
