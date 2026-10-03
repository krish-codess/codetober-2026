"""Failure injection for the external dependency (ESI): retries back off, are capped, honour the error
budget, never retry permanent errors, and a total outage degrades the run instead of crashing it."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from goldstandard.config import Settings
from goldstandard.raw import RawStore
from goldstandard.sources import eve
from goldstandard.sources.esi import EsiClient


def make(handler, **cfg):
    sleeps: list[float] = []
    settings = Settings(http_max_retries=cfg.get("retries", 4), http_backoff_base_s=0.5, http_backoff_cap_s=8.0)
    client = EsiClient(settings, transport=httpx.MockTransport(handler), sleep=sleeps.append)
    return client, sleeps


def test_transient_errors_are_retried_with_capped_exponential_backoff():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 4 else httpx.Response(200, json=[{"ok": 1}])

    client, sleeps = make(handler)
    f = client.get("/markets/1/history/")
    assert f.ok and f.attempts == 4 and len(sleeps) == 3
    assert all(0 <= s <= 8.0 for s in sleeps)


def test_retries_are_capped_and_failure_is_reported_not_raised():
    client, sleeps = make(lambda r: httpx.Response(502), retries=3)
    f = client.get("/markets/1/history/")
    assert not f.ok and f.attempts == 4 and len(sleeps) == 3 and "502" in (f.error or "")


def test_connection_errors_are_retried():
    def handler(request):
        raise httpx.ConnectError("boom")

    client, sleeps = make(handler, retries=2)
    f = client.get("/x/")
    assert not f.ok and len(sleeps) == 2 and "ConnectError" in (f.error or "")


@pytest.mark.parametrize("status", [400, 403, 404])
def test_permanent_errors_are_not_retried(status):
    client, sleeps = make(lambda r: httpx.Response(status))
    f = client.get("/x/")
    assert not f.ok and f.attempts == 1 and sleeps == []


def test_retry_after_and_error_budget_are_honoured():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(420, headers={"Retry-After": "7"})
        return httpx.Response(200, json=[], headers={"X-Esi-Error-Limit-Remain": "3", "X-Esi-Error-Limit-Reset": "5"})

    client, sleeps = make(handler)
    assert client.get("/x/").ok
    assert sleeps == [7.0, 5.0]  # Retry-After, then a pause because the error budget was nearly spent


def test_total_outage_degrades_the_ingest_run_and_writes_nothing(tmp_path):
    universe = {
        "servers": [{"server_id": "eve-domain", "name": "d", "region_id": 1}],
        "items": [{"type_id": 34, "division": "minerals"}, {"type_id": 35, "division": "minerals"}],
    }
    client, _ = make(lambda r: httpx.Response(503), retries=1)
    store = RawStore(tmp_path)
    report = eve.ingest_history(Settings(), store, client=client, universe=universe)
    assert report.requested == 2 and report.stored == 0 and len(report.failures) == 2 and report.degraded
    assert store.days("eve", "market_history") == []


def test_partial_outage_keeps_what_succeeded_and_records_the_gap(tmp_path):
    universe = {
        "servers": [{"server_id": "eve-domain", "name": "d", "region_id": 1}],
        "items": [{"type_id": 34, "division": "minerals"}, {"type_id": 35, "division": "minerals"}],
    }

    def handler(request):
        return httpx.Response(200, json=[]) if request.url.params.get("type_id") == "34" else httpx.Response(500)

    client, _ = make(handler, retries=1)
    store = RawStore(tmp_path)
    report = eve.ingest_order_snapshot(Settings(), store, client=client, universe=universe)
    assert report.stored == 1 and len(report.failures) == 1
    bundle = json.loads(store.read(next(store.iter_refs("eve", "orders", store.days("eve", "orders")[0]))).body)
    by_type = {r["type_id"]: r for r in bundle}
    assert by_type[34]["status"] == 200 and by_type[35]["status"] == 500 and by_type[35]["body"] is None
    assert isinstance(store.days("eve", "orders")[0], date)
