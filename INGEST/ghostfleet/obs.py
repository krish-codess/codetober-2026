"""Structured logs with a correlation id, and every metric the system exports, in one place."""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.request
from contextvars import ContextVar

from prometheus_client import Counter, Gauge, Histogram, start_http_server

# Set per batch (collector, consumer) or per request (API); every log line inside carries it.
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "cid": correlation_id.get(),
            **{k: v for k, v in record.__dict__.items() if k not in _STANDARD},
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("aiokafka").setLevel(logging.WARNING)


# --- collector ---
SENTENCES = Counter("ghost_sentences_total", "Raw lines read from a provider", ["source"])
CONNECTS = Counter(
    "ghost_provider_connects_total", "Connection attempts per provider and outcome", ["source", "outcome"]
)
CONNECTED = Gauge("ghost_provider_connected", "1 while a provider connection is up", ["source"])
PRODUCED = Counter("ghost_messages_produced_total", "Decoded messages acknowledged by Kafka", ["source", "kind"])
QUARANTINED = Counter("ghost_quarantined_total", "Inputs sent to the dead-letter topic", ["stage", "reason"])
IGNORED = Counter(
    "ghost_messages_ignored_total", "Well-formed AIS messages of types this system does not use", ["source"]
)
ARCHIVE_BACKLOG = Gauge("ghost_archive_backlog_bytes", "Archived bytes not yet acknowledged by Kafka", ["source"])
# --- consumer ---
INSERTED = Counter("ghost_positions_inserted_total", "Positions written to PostGIS")
DUPLICATES = Counter("ghost_positions_duplicate_total", "Positions dropped by the (mmsi, ts) key")
BATCHES = Histogram(
    "ghost_sink_batch_seconds",
    "Time to write one consumer batch",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
INGEST_LAG = Histogram(
    "ghost_ingest_lag_seconds", "Receive at the collector to committed in PostGIS",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300),
)  # fmt: skip
# --- detector ---
DETECT_RUNS = Histogram(
    "ghost_detect_seconds", "Duration of one detector pass", buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60)
)
DARK_EVENTS = Gauge("ghost_dark_events", "Events currently in the table", ["classification"])
# --- everyone ---
LAST_SUCCESS = Gauge(
    "ghost_last_success_timestamp_seconds", "Last time this worker completed a round trip to its dependencies"
)


def serve_metrics(port: int) -> None:
    if port:
        start_http_server(port)


def health(url: str, max_age_s: float) -> bool:
    """Container health check: scrape our own metrics and require a recent dependency round trip."""
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - operator-supplied localhost URL
        for line in r.read().decode().splitlines():
            if line.startswith("ghost_last_success_timestamp_seconds "):
                return time.time() - float(line.split()[1]) <= max_age_s
    return False
