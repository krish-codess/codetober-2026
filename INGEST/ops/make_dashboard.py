"""Writes ops/grafana/dashboards/ingest.json. The dashboard is code: edit here, rerun, commit both."""

import json
from pathlib import Path


def panel(i, title, exprs, x, y, unit="short", kind="timeseries", w=12, h=8):
    return {
        "id": i, "title": title, "type": kind, "datasource": {"type": "prometheus", "uid": "prometheus"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h}, "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [{"refId": chr(65 + n), "expr": e, "legendFormat": legend} for n, (e, legend) in enumerate(exprs)],
    }  # fmt: skip


def lag(q):
    return f"histogram_quantile({q}, sum by (le) (rate(ghost_ingest_lag_seconds_bucket[5m])))"


PANELS = [
    panel(1, "Providers connected", [("ghost_provider_connected", "{{source}}")], 0, 0, kind="stat", w=6, h=5),
    panel(2, "Positions stored per second", [("sum(rate(ghost_positions_inserted_total[1m]))", "stored")], 6, 0, "ops", "stat", 6, 5),
    panel(3, "Ingest lag p95 (budget 2 s)", [(lag(0.95), "p95")], 12, 0, "s", "stat", 6, 5),
    panel(4, "Dark events in the table", [("ghost_dark_events", "{{classification}}")], 18, 0, kind="stat", w=6, h=5),
    panel(5, "Ingest lag: received by collector to stored in PostGIS", [(lag(0.5), "p50"), (lag(0.95), "p95"), (lag(0.99), "p99")], 0, 5, "s"),
    panel(6, "Throughput by stage", [
        ("sum by (source) (rate(ghost_sentences_total[1m]))", "received {{source}}"),
        ("sum by (kind) (rate(ghost_messages_produced_total[1m]))", "produced {{kind}}"),
        ("sum(rate(ghost_positions_inserted_total[1m]))", "stored"),
        ("sum(rate(ghost_positions_duplicate_total[1m]))", "deduplicated"),
    ], 12, 5, "ops"),
    panel(7, "Archive not yet acknowledged by Kafka", [("ghost_archive_backlog_bytes", "{{source}}")], 0, 13, "bytes"),
    panel(8, "Quarantined per second, by reason", [("sum by (reason) (rate(ghost_quarantined_total[5m]))", "{{reason}}")], 12, 13, "ops"),
    panel(9, "Provider connection attempts", [("sum by (source, outcome) (increase(ghost_provider_connects_total[5m]))", "{{source}} {{outcome}}")], 0, 21),
    panel(10, "Sink batch time and detector pass time (p95)", [
        ("histogram_quantile(0.95, sum by (le) (rate(ghost_sink_batch_seconds_bucket[5m])))", "sink batch"),
        ("histogram_quantile(0.95, sum by (le) (rate(ghost_detect_seconds_bucket[15m])))", "detector pass"),
    ], 12, 21, "s"),
    panel(11, "API latency p95 by route", [("histogram_quantile(0.95, sum by (le, route) (rate(ghost_http_request_seconds_bucket[5m])))", "{{route}}")], 0, 29, "s"),
    panel(12, "API responses by status", [("sum by (status) (rate(ghost_http_request_seconds_count[1m]))", "{{status}}")], 12, 29, "reqps"),
]  # fmt: skip

DASHBOARD = {
    "uid": "ghostfleet", "title": "Ghost Fleet: ingest", "schemaVersion": 39, "version": 1, "refresh": "10s",
    "time": {"from": "now-1h", "to": "now"}, "timezone": "utc", "tags": ["ghostfleet"], "panels": PANELS,
}  # fmt: skip

if __name__ == "__main__":
    out = Path(__file__).parent / "grafana" / "dashboards" / "ingest.json"
    out.write_text(json.dumps(DASHBOARD, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(PANELS)} panels)")
