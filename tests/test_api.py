"""API contract tests against the seeded test database: success, validation failure, auth failure and
malformed input for every endpoint."""

from __future__ import annotations

import hashlib
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from goldstandard.config import settings

pytestmark = pytest.mark.integration
TOKEN = "gsk_test_" + "x" * 32


@pytest.fixture(scope="module")
def client(seeded, monkeypatch_module):
    monkeypatch_module.setenv("GS_API_DATABASE_URL", seeded["api"])
    monkeypatch_module.setenv("GS_LOG_JSON", "false")
    settings.cache_clear()
    with psycopg.connect(seeded["owner"]) as c:
        c.execute(
            """INSERT INTO api_key (key_id, key_hash, scopes) VALUES ('writer', %s, '{patches:write}'),
                     ('reader', %s, '{}') ON CONFLICT DO NOTHING""",
            (hashlib.sha256(TOKEN.encode()).hexdigest(), hashlib.sha256(b"gsk_reader_token_123456").hexdigest()),
        )
    from goldstandard.api.app import app

    with TestClient(app) as tc:
        yield tc
    settings.cache_clear()


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def assert_error(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()["error"]
    assert body["code"] == code and body["request_id"] and "Traceback" not in r.text


# ------------------------------------------------------------------------------------------ ops
def test_health_and_readiness_exercise_the_database(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["postgres"]["ok"] and body["checks"]["postgres"]["latency_ms"] is not None
    assert set(body["freshness"]) == {"eve", "synthetic"}
    assert body["status"] == "degraded"  # test data ends in 2025/26: it is stale and readiness must say so


def test_metrics_and_correlation_id(client):
    r = client.get("/v1/worlds", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"
    assert 'http_requests_total{method="GET",route="/v1/worlds",status="200"}' in client.get("/metrics").text


# ------------------------------------------------------------------------------------------ reads
def test_worlds(client):
    worlds = {w["world_id"]: w for w in client.get("/v1/worlds").json()}
    assert worlds["eve"]["price_source"] == "trade_history" and not worlds["eve"]["is_synthetic"]
    assert {s["server_id"] for s in worlds["synthetic"]["servers"]} == {"syn-aurora", "syn-borealis"}
    assert worlds["synthetic"]["activities"]


def test_index_success_etag_and_validation(client):
    r = client.get("/v1/index", params={"world": "eve", "from": "2025-10-01", "to": "2025-10-31"})
    assert r.status_code == 200
    pts = r.json()["points"]
    assert len(pts) == 31 and all(p["vintage"] >= 1 for p in pts)
    assert (
        client.get(
            "/v1/index",
            params={"world": "eve", "from": "2025-10-01", "to": "2025-10-31"},
            headers={"If-None-Match": r.headers["ETag"]},
        ).status_code
        == 304
    )
    assert_error(client.get("/v1/index", params={"world": "nope"}), 404, "unknown_world")
    assert_error(client.get("/v1/index", params={"world": "eve", "server": "syn-aurora"}), 404, "unknown_series")
    assert_error(
        client.get("/v1/index", params={"world": "eve", "from": "2025-10-31", "to": "2025-10-01"}), 400, "bad_range"
    )
    assert_error(
        client.get("/v1/index", params={"world": "eve", "from": "2010-01-01", "to": "2025-10-01"}),
        400,
        "range_too_large",
    )
    assert_error(client.get("/v1/index", params={"world": "eve", "from": "not-a-date"}), 422, "validation_error")
    assert_error(client.get("/v1/index", params={"world": "EVE'; DROP TABLE world;--"}), 422, "validation_error")
    assert_error(client.get("/v1/index"), 422, "validation_error")


def test_index_as_of_and_revisions(client):
    r = client.get("/v1/index", params={"world": "synthetic", "as_of": "2000-01-01T00:00:00Z"})
    assert r.status_code == 200 and r.json()["points"] == []  # nothing had been published in 2000
    day = client.get("/v1/index", params={"world": "synthetic"}).json()["points"][0]["day"]
    rev = client.get("/v1/index/revisions", params={"world": "synthetic", "day": day}).json()
    assert rev["revisions"][0]["vintage"] == 1 and rev["revisions"][0]["reason"] == "initial"
    assert_error(client.get("/v1/index/revisions", params={"world": "synthetic", "day": "1999-01-01"}), 404, "no_value")


def test_inflation_endpoints(client):
    r = client.get("/v1/inflation", params={"world": "eve", "window": 30})
    assert r.status_code == 200 and r.json()["points"]
    m = client.get("/v1/inflation/matrix", params={"world": "eve", "window": 7}).json()
    assert {c["server_id"] for c in m["cells"]} >= {"all", "eve-the-forge"}
    assert_error(client.get("/v1/inflation", params={"world": "eve", "window": 12}), 400, "bad_window")
    assert_error(client.get("/v1/inflation/matrix", params={"world": "eve", "window": 5}), 400, "bad_window")


def test_patches_keyset_pagination_is_complete_and_stable(client):
    seen, cursor = [], None
    while True:
        params = {"world": "eve", "limit": 37, **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/patches", params=params).json()
        seen += [p["patch_id"] for p in page["items"]]
        cursor = page["page"]["next_cursor"]
        if not cursor:
            break
    full = client.get("/v1/patches", params={"world": "eve", "limit": 200}).json()
    assert len(seen) == len(set(seen)) > 37  # no duplicates across pages, more than one page
    assert seen[: len(full["items"])] == [p["patch_id"] for p in full["items"]]
    assert_error(client.get("/v1/patches", params={"world": "eve", "cursor": "%%%"}), 400, "invalid_cursor")
    assert_error(client.get("/v1/patches", params={"world": "eve", "cursor": "eyJ4IjoxfQ"}), 400, "invalid_cursor")
    assert_error(client.get("/v1/patches", params={"world": "eve", "limit": 0}), 422, "validation_error")


def test_shocks(client):
    r = client.get("/v1/shocks", params={"world": "eve", "division": "fuel"})
    assert r.status_code == 200
    for s in r.json()["shocks"]:
        assert s["direction"] in ("up", "down")
        assert [a["rank"] for a in s["attributions"]] == list(range(1, len(s["attributions"]) + 1))
    assert_error(client.get("/v1/shocks", params={"world": "eve", "division": "bogus"}), 404, "unknown_series")


def test_purchasing_power(client):
    r = client.get(
        "/v1/purchasing-power",
        params={"world": "synthetic", "server": "syn-aurora", "activity": "syn-ratting", "items": "587,34"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["points"] and len(body["rates"]) >= 1
    p = body["points"][-1]
    assert p["wage"] > 0 and p["hours_per_basket"] > 0
    for it in p["items"]:
        if it["price"]:
            assert abs(it["units_per_hour"] * it["hours_per_unit"] - 1) < 1e-9
    assert_error(
        client.get("/v1/purchasing-power", params={"world": "synthetic", "server": "syn-aurora", "activity": "nope"}),
        404,
        "unknown_activity",
    )
    assert_error(
        client.get(
            "/v1/purchasing-power",
            params={"world": "synthetic", "server": "syn-aurora", "activity": "syn-ratting", "items": "1,2,x"},
        ),
        422,
        "validation_error",
    )
    assert_error(
        client.get(
            "/v1/purchasing-power",
            params={"world": "synthetic", "server": "syn-aurora", "activity": "syn-ratting", "items": "999999"},
        ),
        404,
        "unknown_item",
    )


def test_manipulation_feed_paginates(client):
    first = client.get("/v1/manipulation", params={"world": "synthetic", "server": "syn-borealis", "limit": 5}).json()
    assert len(first["items"]) == 5 and first["page"]["next_cursor"]
    second = client.get(
        "/v1/manipulation",
        params={"world": "synthetic", "server": "syn-borealis", "limit": 5, "cursor": first["page"]["next_cursor"]},
    ).json()
    a = [(i["day"], i["item_id"], i["kind"]) for i in first["items"]]
    b = [(i["day"], i["item_id"], i["kind"]) for i in second["items"]]
    assert not set(a) & set(b) and a + b == sorted(a + b, reverse=True)
    assert_error(
        client.get("/v1/manipulation", params={"world": "synthetic", "server": "syn-borealis", "kind": "fraud"}),
        422,
        "validation_error",
    )


def test_money_flows(client):
    r = client.get("/v1/money-flows", params={"world": "synthetic", "server": "syn-aurora"}).json()
    assert r["points"] and all(p["sinks"] > 0 for p in r["points"])
    reported = [p for p in r["points"] if p["faucets"] is not None]
    assert reported and all(p["net"] == pytest.approx(p["faucets"] - p["sinks"]) for p in reported)
    # a day whose faucet report has not arrived yet is null, never a fabricated zero
    assert all(p["net"] is None for p in r["points"] if p["faucets"] is None)
    eve = client.get("/v1/money-flows", params={"world": "eve", "server": "eve-the-forge"}).json()
    assert eve["points"] and all(p["faucets"] is None for p in eve["points"])  # real EVE has no faucet feed


# ------------------------------------------------------------------------------------------ writes
def _patch(**kw):
    return {
        "world_id": "synthetic",
        "patch_id": f"api-{uuid.uuid4().hex[:8]}",
        "released_at": "2025-10-05T11:00:00Z",
        "title": "Hotfix: mining yields reverted",
        "notes": "Ore and mining changes",
        **kw,
    }


def test_post_patch_requires_auth_and_scope(client):
    assert_error(client.post("/v1/patches", json=_patch()), 401, "unauthenticated")
    assert_error(
        client.post("/v1/patches", json=_patch(), headers={"Authorization": "Bearer wrong"}), 401, "unauthenticated"
    )
    assert_error(
        client.post(
            "/v1/patches",
            json=_patch(),
            headers={"Authorization": "Bearer gsk_reader_token_123456", "Idempotency-Key": "k" * 10},
        ),
        403,
        "forbidden",
    )


def test_post_patch_is_idempotent(client):
    body = _patch()
    h = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": uuid.uuid4().hex}
    r1 = client.post("/v1/patches", json=body, headers=h)
    assert r1.status_code == 201, r1.text and r1.json()["tags"] == ["minerals"]
    r2 = client.post("/v1/patches", json=body, headers=h)
    assert r2.status_code == 200 and r2.headers["Idempotent-Replayed"] == "true" and r2.json() == r1.json()
    assert_error(
        client.post("/v1/patches", json={**body, "title": "different"}, headers=h), 409, "idempotency_conflict"
    )
    assert_error(
        client.post("/v1/patches", json=body, headers={**h, "Idempotency-Key": uuid.uuid4().hex}), 409, "patch_exists"
    )
    assert_error(
        client.post("/v1/patches", json=_patch(), headers={"Authorization": f"Bearer {TOKEN}"}),
        400,
        "idempotency_key_required",
    )


def test_post_patch_validates_input(client):
    h = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": uuid.uuid4().hex}
    assert_error(client.post("/v1/patches", json=_patch(patch_id="bad id!"), headers=h), 422, "validation_error")
    assert_error(client.post("/v1/patches", json=_patch(extra="x"), headers=h), 422, "validation_error")
    assert_error(client.post("/v1/patches", json=_patch(title=""), headers=h), 422, "validation_error")
    assert_error(
        client.post("/v1/patches", content=b"{not json", headers={**h, "Content-Type": "application/json"}),
        422,
        "validation_error",
    )
    assert_error(client.post("/v1/patches", json=_patch(world_id="atlantis"), headers=h), 404, "unknown_world")


def test_unknown_route_and_method(client):
    assert_error(client.get("/v1/nothing"), 404, "not_found")
    assert_error(client.delete("/v1/worlds"), 405, "method_not_allowed")
