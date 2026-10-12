"""Every endpoint: success, validation failure, malformed input, not found, and a dead database.

There is no authorization-failure case because there is no authorization: the API is read-only and
public by design (docs/decisions.md). What stands in for it is the proof that nothing can be written.
"""

import dataclasses
import json

import asyncpg
import httpx
import pytest
import pytest_asyncio
from conftest import TABLES, load

from ghostfleet.api import create_app
from ghostfleet.detect import detect, refresh_coverage

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def conn(cfg, schema, history):
    """One loaded database for the whole module: these tests only read. Shadows the truncating fixture."""
    positions, statics, dead, _ = history
    c = await asyncpg.connect(cfg.dsn)
    await c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    await load(c, positions[:60_000], statics, dead[:500])
    await refresh_coverage(c, cfg)
    await detect(c, cfg)
    yield c
    await c.close()


@pytest_asyncio.fixture
async def api(cfg, conn):
    app = create_app(cfg)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        yield client
    if getattr(app.state, "pool", None):
        await app.state.pool.close()


def assert_error(r: httpx.Response, status: int, code: str) -> dict:
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error"} and body["error"]["code"] == code
    assert body["error"]["request_id"] == r.headers["x-request-id"]
    assert "Traceback" not in r.text
    return body["error"]


async def test_health_and_readiness(api):
    assert (await api.get("/healthz")).json() == {"status": "alive"}
    ready = (await api.get("/readyz")).json()
    assert ready["database"] == "ok" and ready["newest_position_at"].startswith("2026-10-09")
    assert "ghost_http_request_seconds" in (await api.get("/metrics")).text


async def test_request_id_is_echoed_or_generated(api):
    assert (await api.get("/healthz", headers={"X-Request-ID": "trace-123"})).headers["x-request-id"] == "trace-123"
    generated = (await api.get("/healthz", headers={"X-Request-ID": "x" * 500})).headers["x-request-id"]
    assert len(generated) == 32  # an oversized id is not reflected back


async def test_live_returns_latest_positions_and_supports_delta_polling(api):
    r = await api.get("/api/v1/vessels/live")
    body = r.json()
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert len(body["vessels"]) > 300 and not body["truncated"]
    mmsis = [v[0] for v in body["vessels"]]
    assert mmsis == sorted(set(mmsis))
    assert all(-180 <= v[1] <= 180 and -90 <= v[2] <= 90 for v in body["vessels"])
    # Nothing has changed since the server's own clock reading: the delta is empty.
    assert (await api.get("/api/v1/vessels/live", params={"since": body["server_time"]})).json()["vessels"] == []
    boxed = (await api.get("/api/v1/vessels/live", params={"bbox": "0,50,10,62"})).json()["vessels"]
    assert 0 < len(boxed) < len(mmsis) and all(0 <= v[1] <= 10 and 50 <= v[2] <= 62 for v in boxed)
    cut = (await api.get("/api/v1/vessels/live", params={"limit": 5})).json()
    assert len(cut["vessels"]) == 5 and cut["truncated"]


@pytest.mark.parametrize(
    ("path", "params", "status", "code"),
    [
        ("/api/v1/vessels/live", {"bbox": "1,2,3"}, 400, "bad_request"),
        ("/api/v1/vessels/live", {"bbox": "a,b,c,d"}, 400, "bad_request"),
        ("/api/v1/vessels/live", {"bbox": "10,50,0,62"}, 400, "bad_request"),
        ("/api/v1/vessels/live", {"bbox": "0,50,10,95"}, 400, "bad_request"),
        ("/api/v1/vessels/live", {"limit": 0}, 422, "invalid_request"),
        ("/api/v1/vessels/live", {"limit": 10**9}, 422, "invalid_request"),
        ("/api/v1/vessels/live", {"since": "yesterday"}, 422, "invalid_request"),
        ("/api/v1/vessels/live", {"max_age_h": -1}, 422, "invalid_request"),
        ("/api/v1/vessels/trails", {}, 422, "invalid_request"),
        ("/api/v1/vessels/trails", {"bbox": "0,50,10,62", "minutes": 100000}, 422, "invalid_request"),
        ("/api/v1/vessels/search", {"q": "x"}, 422, "invalid_request"),
        ("/api/v1/vessels/abc", {}, 422, "invalid_request"),
        ("/api/v1/vessels/123", {}, 422, "invalid_request"),
        ("/api/v1/vessels/257999999", {}, 404, "not_found"),
        ("/api/v1/vessels/257999999/track", {}, 404, "not_found"),
        ("/api/v1/vessels/257000001/track", {"cursor": "!!!"}, 400, "bad_request"),
        (
            "/api/v1/vessels/257000001/track",
            {"cursor": "WyJhIl0="},
            400,
            "bad_request",
        ),  # ["a"]: right shape, wrong type
        (
            "/api/v1/vessels/257000001/track",
            {"start": "2026-10-09T12:00:00Z", "end": "2026-10-09T11:00:00Z"},
            400,
            "bad_request",
        ),
        ("/api/v1/dark-events", {"classification": "ufo"}, 422, "invalid_request"),
        ("/api/v1/dark-events", {"min_score": 101}, 422, "invalid_request"),
        ("/api/v1/dark-events", {"limit": 501}, 422, "invalid_request"),
        ("/api/v1/dark-events", {"cursor": "WzEsIDJd"}, 400, "bad_request"),  # [1, 2]: score must be a float
        ("/api/v1/dark-events", {"mmsi": "1; DROP TABLE positions"}, 422, "invalid_request"),
        ("/api/v1/dark-events/0", {}, 422, "invalid_request"),
        ("/api/v1/dark-events/999999999", {}, 404, "not_found"),
        ("/api/v1/coverage", {"bbox": "nonsense"}, 400, "bad_request"),
        ("/api/v1/nope", {}, 404, "not_found"),
    ],
)
async def test_bad_input_gets_a_structured_error(api, path, params, status, code):
    error = assert_error(await api.get(path, params=params), status, code)
    assert error["message"]
    if status == 422:
        assert error["details"] and all({"field", "problem"} == set(d) for d in error["details"])


async def test_the_api_cannot_be_used_to_write(api):
    for method in ("post", "put", "patch", "delete"):
        assert_error(await getattr(api, method)("/api/v1/dark-events"), 405, "method_not_allowed")
        assert_error(await getattr(api, method)("/api/v1/vessels/257000001"), 405, "method_not_allowed")


async def test_search_escapes_wildcards_and_finds_by_name_or_mmsi_prefix(api, conn):
    name = await conn.fetchval("SELECT name FROM vessels WHERE name IS NOT NULL ORDER BY mmsi LIMIT 1")
    hits = (await api.get("/api/v1/vessels/search", params={"q": name[:6].lower()})).json()
    assert hits and all(name[:6] in h["name"] for h in hits) and len(hits) <= 20
    assert (await api.get("/api/v1/vessels/search", params={"q": "%%"})).json() == []  # a literal %, not match-all
    by_mmsi = (await api.get("/api/v1/vessels/search", params={"q": "257"})).json()
    assert by_mmsi and all(str(h["mmsi"]).startswith("257") and h["flag"] == "NO" for h in by_mmsi)


async def test_vessel_detail_and_track_pagination(api, conn):
    mmsi = await conn.fetchval("SELECT mmsi FROM positions GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
    total = await conn.fetchval("SELECT count(*) FROM positions WHERE mmsi = $1", mmsi)
    v = (await api.get(f"/api/v1/vessels/{mmsi}")).json()
    assert v["mmsi"] == mmsi and v["flag"] and v["last_seen"] and v["lat"] is not None

    seen, cursor, pages = [], None, 0
    while True:
        page = (
            await api.get(
                f"/api/v1/vessels/{mmsi}/track", params={"limit": 40, **({"cursor": cursor} if cursor else {})}
            )
        ).json()
        seen += [p[0] for p in page["points"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages > 2 and len(seen) == total and seen == sorted(set(seen))  # every point once, in order
    window = (
        await api.get(
            f"/api/v1/vessels/{mmsi}/track", params={"start": "2026-10-09T12:30:00Z", "end": "2026-10-09T13:30:00Z"}
        )
    ).json()
    assert window["points"] and all(1791549000 <= p[0] < 1791552600 for p in window["points"])
    naive = (
        await api.get(
            f"/api/v1/vessels/{mmsi}/track", params={"start": "2026-10-09T12:30:00", "end": "2026-10-09T13:30:00"}
        )
    ).json()
    assert naive == window  # a timestamp without a zone is read as UTC


async def test_dark_events_are_ranked_and_pages_neither_skip_nor_repeat(api, conn):
    total = await conn.fetchval("SELECT count(*) FROM dark_events")
    assert total > 20
    items, cursor = [], None
    while True:
        page = (
            await api.get("/api/v1/dark-events", params={"limit": 7, **({"cursor": cursor} if cursor else {})})
        ).json()
        assert len(page["items"]) <= 7
        items += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(items) == total == len({e["id"] for e in items})
    assert [(-e["score"], -e["id"]) for e in items] == sorted((-e["score"], -e["id"]) for e in items)
    top = items[0]
    assert top["reasons"] and top["start"] and top["classification"] in ("dark", "coverage_gap", "position_jump")
    assert (await api.get(f"/api/v1/dark-events/{top['id']}")).json() == top

    dark = (await api.get("/api/v1/dark-events", params={"classification": "dark", "limit": 500})).json()["items"]
    assert dark and {e["classification"] for e in dark} == {"dark"}
    strong = (await api.get("/api/v1/dark-events", params={"min_score": 50, "limit": 500})).json()["items"]
    assert all(e["score"] >= 50 for e in strong)
    impossible = (await api.get("/api/v1/dark-events", params={"impossible_only": True, "limit": 500})).json()["items"]
    assert all(e["impossible_speed"] and e["implied_speed_kn"] > 50 for e in impossible)
    one = (await api.get("/api/v1/dark-events", params={"mmsi": top["mmsi"]})).json()["items"]
    assert one and {e["mmsi"] for e in one} == {top["mmsi"]}


async def test_trails_coverage_and_stats(api, conn):
    trails = (
        await api.get("/api/v1/vessels/trails", params={"bbox": "-180,-90,180,90", "minutes": 30, "max_vessels": 50})
    ).json()
    assert trails["truncated"] and len(trails["trails"]) == 50
    assert all(pts == sorted(pts, key=lambda p: p[2]) for pts in trails["trails"].values())
    cov = await api.get("/api/v1/coverage")
    assert cov.headers["cache-control"] == "public, max-age=300"
    cells = cov.json()
    assert cells["cell_deg"] == 0.25 and len(cells["cells"]) > 100 and all(0 <= c[2] <= 1 for c in cells["cells"])
    stats = (await api.get("/api/v1/stats")).json()
    assert stats["vessels"] == await conn.fetchval("SELECT count(*) FROM vessel_latest")
    assert stats["quarantine"] and sum(stats["events"].values()) == await conn.fetchval(
        "SELECT count(*) FROM dark_events"
    )


async def test_database_down_is_a_503_with_retry_advice_not_a_stack_trace(cfg):
    app = create_app(dataclasses.replace(cfg, dsn="postgresql://nobody@127.0.0.1:1/none"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        assert (await client.get("/healthz")).status_code == 200  # alive, though not ready
        for path in ("/readyz", "/api/v1/vessels/live", "/api/v1/dark-events", "/api/v1/stats"):
            r = await client.get(path)
            assert_error(r, 503, "unavailable")
            assert r.headers["retry-after"] == "5"


async def test_openapi_document_is_generated_from_the_code_and_committed(cfg):
    from pathlib import Path

    committed = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
    generated = json.dumps(create_app(cfg).openapi(), indent=2, sort_keys=True) + "\n"
    assert committed.read_text(encoding="utf-8") == generated, "run: python -m ghostfleet openapi docs/openapi.json"
    doc = json.loads(generated)
    assert set(doc["paths"]) >= {
        "/api/v1/vessels/live",
        "/api/v1/vessels/{mmsi}/track",
        "/api/v1/dark-events",
        "/readyz",
    }
    assert "Error" in doc["components"]["schemas"]
