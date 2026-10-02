"""Observability: structured JSON logs carrying a correlation id, plus in-process metrics."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", as_json: bool = True) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if as_json else logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})


class Metrics:
    """Counters and timing summaries. Exposed in Prometheus text format by the API."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self.timings: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self.counters[(name, tuple(sorted(labels.items())))] += value

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        with self._lock:
            bucket = self.timings[(name, tuple(sorted(labels.items())))]
            bucket.append(seconds)
            del bucket[:-1000]  # ponytail: rolling window of 1000 samples, use a real histogram if needed

    @contextmanager
    def timer(self, name: str, **labels: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - t0, **labels)

    def render_prometheus(self) -> str:
        def fmt(labels: tuple[tuple[str, str], ...]) -> str:
            if not labels:
                return ""
            return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"

        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self.counters.items()):
                lines.append(f"{name}_total{fmt(labels)} {value}")
            for (name, labels), samples in sorted(self.timings.items()):
                s = sorted(samples)
                for q in (0.5, 0.95, 0.99):
                    ql = (*labels, ("quantile", str(q)))
                    lines.append(f"{name}_seconds{fmt(ql)} {s[min(len(s) - 1, int(q * len(s)))]:.6f}")
                lines.append(f"{name}_seconds_count{fmt(labels)} {len(s)}")
        return "\n".join(lines) + "\n"


metrics = Metrics()
