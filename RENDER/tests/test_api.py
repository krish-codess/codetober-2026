from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dots.api import create_app, points_in_polygon
from dots.tiles import load_variant

from .conftest import make_settings

SQUARE = [[-1e6, -1e6], [1e6, -1e6], [1e6, 1e6], [-1e6, 1e6]]


def is_error(r, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    body = r.json()
    assert body["error"]["code"] == code and body["error"]["message"]
    assert body["request_id"] == r.headers["x-request-id"]
    assert "Traceback" not in r.text


def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz")
    assert r.status_code == 200
    assert set(r.json()["checks"]) == {"manifest", "tiles", "metadata"}
    assert all(c["ok"] for c in r.json()["checks"].values())


def test_readiness_fails_when_a_dependency_is_actually_broken(site_copy, built):
    v = built[2]["variants"][0]
    root = site_copy.site_dir / "data" / v["tiles_path"] / "0-0-0.bin"
    root.write_bytes(root.read_bytes()[:-7])  # a truncated upload
    c = TestClient(create_app(site_copy))
    r = c.get("/readyz")
    assert r.status_code == 503
    checks = r.json()["checks"]
    assert checks["manifest"]["ok"] and checks["metadata"]["ok"] and not checks["tiles"]["ok"]
    is_error(
        c.post("/api/v1/summary", json={"variant": v["name"], "polygon": SQUARE}), 503, "tiles_unreadable"
    )


def test_no_published_data_is_a_503_that_says_what_to_do(tmp_path):
    c = TestClient(create_app(make_settings(tmp_path)))
    is_error(c.get("/api/v1/variants"), 503, "no_data")
    assert c.get("/readyz").status_code == 503
    assert c.get("/healthz").status_code == 200


def test_variants(client, built):
    body = client.get("/api/v1/variants").json()
    assert body["build_id"] == built[2]["build_id"] and body["synthetic"] is True
    assert [v["name"] for v in body["variants"]] == ["pca", "umap-n10-d0.1"]
    assert body["variants"][1]["metrics"]["recall"]["all"]["n"] > 0


def test_summary_of_everything_matches_the_manifest(client, built):
    m = built[2]
    r = client.post("/api/v1/summary", json={"variant": "pca", "polygon": SQUARE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n"] == body["total"] == m["n_points"]
    for col, expected in zip(body["columns"], m["columns"], strict=True):
        assert {c["label"]: c["count"] for c in col["counts"]} == {
            lab: n for lab, n in zip(expected["labels"], expected["counts"], strict=True) if n
        }
        assert all(abs(c["lift"] - 1) < 1e-9 for c in col["counts"]), (
            "the whole dataset is not enriched in anything"
        )
        assert 0 < col["purity"] <= 1 and 0 <= col["entropy"] <= 1
    assert body["rank"]["min"] == 1 and len(body["top_tokens"]) == 10
    assert [t["rank"] for t in body["top_tokens"]] == sorted(t["rank"] for t in body["top_tokens"])


def test_summary_filters_and_empty_selection(client, built):
    script = built[2]["columns"][0]
    top = script["labels"][1]
    r = client.post(
        "/api/v1/summary", json={"variant": "pca", "polygon": SQUARE, "filters": {"script": [top]}}
    ).json()
    assert r["n"] == script["counts"][1]
    assert r["columns"][0]["purity"] == 1.0 and r["columns"][0]["entropy"] == 0.0
    empty = client.post(
        "/api/v1/summary", json={"variant": "pca", "polygon": [[9e8, 9e8], [9e8 + 1, 9e8], [9e8, 9e8 + 1]]}
    ).json()
    assert (
        empty["n"] == 0
        and empty["rank"] is None
        and empty["top_tokens"] == []
        and empty["columns"][0]["purity"] is None
    )


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"variant": "nope", "polygon": SQUARE}, 404, "unknown_variant"),
        ({"variant": "pca", "polygon": SQUARE[:2]}, 422, "invalid_request"),
        ({"variant": "pca", "polygon": [[0, 0]] * 3000}, 422, "invalid_request"),
        ({"variant": "pca", "polygon": [[0, "a"], [1, 1], [2, 2]]}, 422, "invalid_request"),
        ({"variant": "pca"}, 422, "invalid_request"),
        ({"variant": "pca", "polygon": SQUARE, "filters": {"token": ["x"]}}, 422, "invalid_request"),
        (
            {"variant": "pca", "polygon": SQUARE, "filters": {"script": ["Klingon'; DROP TABLE meta; --"]}},
            422,
            "invalid_request",
        ),
    ],
)
def test_summary_rejects_bad_input_with_a_structured_error(client, body, status, code):
    is_error(client.post("/api/v1/summary", json=body), status, code)


def test_malformed_and_unroutable_requests(client):
    is_error(
        client.post("/api/v1/summary", content=b"{not json", headers={"content-type": "application/json"}),
        422,
        "invalid_request",
    )
    is_error(
        client.post(
            "/api/v1/summary",
            content=b'{"variant": "pca", "polygon": [[1e999, 0], [0, 1], [1, 1]]}',
            headers={"content-type": "application/json"},
        ),
        422,
        "invalid_request",
    )
    is_error(client.get("/api/v1/nothing"), 404, "not_found")
    is_error(client.get("/data/../../etc/passwd"), 404, "not_found")
    assert client.delete("/api/v1/variants").status_code == 405


def test_points_pages_walk_the_whole_box_in_id_order(client, built):
    s, _, m = built
    pts = load_variant(s.site_dir, m, "pca")
    x0, x1 = np.percentile(pts["x"], [20, 80])
    y0, y1 = np.percentile(pts["y"], [20, 80])
    expected = sorted(
        pts["id"][(pts["x"] >= x0) & (pts["x"] <= x1) & (pts["y"] >= y0) & (pts["y"] <= y1)].tolist()
    )
    got, cursor, pages = [], None, 0
    while True:
        params = {
            "variant": "pca",
            "bbox": f"{x0},{y0},{x1},{y1}",
            "limit": 500,
            **({"cursor": cursor} if cursor else {}),
        }
        body = client.get("/api/v1/points", params=params).json()
        got += [p["id"] for p in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert got == expected and pages == -(-len(expected) // 500) and pages > 1
    first = client.get(
        "/api/v1/points", params={"variant": "pca", "bbox": f"{x0},{y0},{x1},{y1}", "limit": 1}
    ).json()["items"][0]
    assert set(first) == {"id", "token", "rank", "x", "y", "script", "kind", "cluster"}


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"variant": "pca", "bbox": "0,0,1"}, 422, "invalid_request"),
        ({"variant": "pca", "bbox": "0,0,1,1", "limit": 0}, 422, "invalid_request"),
        ({"variant": "pca", "bbox": "0,0,1,1", "limit": 100000}, 422, "invalid_request"),
        ({"variant": "pca", "bbox": "0,0,1,1; DROP TABLE"}, 422, "invalid_request"),
        ({"variant": "pca", "bbox": "0,0,1,1", "cursor": "not-a-cursor"}, 400, "invalid_cursor"),
        ({"variant": "nope", "bbox": "0,0,1,1"}, 404, "unknown_variant"),
        ({"bbox": "0,0,1,1"}, 422, "invalid_request"),
    ],
)
def test_points_rejects_bad_input(client, params, status, code):
    is_error(client.get("/api/v1/points", params=params), status, code)


def test_a_cursor_from_an_older_build_is_refused(client):
    from dots.api import encode_cursor

    is_error(
        client.get(
            "/api/v1/points", params={"variant": "pca", "bbox": "0,0,1,1", "cursor": encode_cursor("0ld", 5)}
        ),
        400,
        "stale_cursor",
    )


def test_tiles_are_served_as_immutable_binary_and_the_manifest_is_not_cached(client, built):
    m = built[2]
    assert client.get("/data/manifest.json").headers["cache-control"] == "no-cache"
    r = client.get(f"/data/{m['variants'][0]['tiles_path']}/0-0-0.bin")
    assert r.status_code == 200 and r.content[:4] == b"DOTS"
    assert "immutable" in r.headers["cache-control"]
    assert client.get(f"/data/{m['meta']['path']}", headers={"range": "bytes=0-3"}).content == b"PAR1"


def test_request_ids_and_metrics(client):
    assert client.get("/healthz", headers={"x-request-id": "abc-123"}).headers["x-request-id"] == "abc-123"
    assert (
        client.get("/healthz", headers={"x-request-id": "bad id\twith junk"}).headers["x-request-id"]
        != "bad id\twith junk"
    )
    client.post("/api/v1/summary", json={"variant": "nope", "polygon": SQUARE})
    text = client.get("/metrics").text
    assert 'dots_requests_total{route="/healthz",status="200"} 2' in text
    assert 'dots_requests_total{route="/api/v1/summary",status="404"} 1' in text
    assert 'dots_request_seconds_bucket{route="/healthz",le="+Inf"} 2' in text


def test_an_unexpected_failure_is_a_clean_500(built, monkeypatch):
    c = TestClient(create_app(built[0]), raise_server_exceptions=False)
    monkeypatch.setattr("dots.api.summarise_counts", lambda *a: 1 / 0)
    is_error(c.post("/api/v1/summary", json={"variant": "pca", "polygon": SQUARE}), 500, "internal_error")


def test_cors_is_off_unless_configured(built):
    s = dataclasses.replace(built[0], cors_origins=("https://example.org",))
    allowed = TestClient(create_app(s)).get("/healthz", headers={"origin": "https://example.org"})
    assert allowed.headers["access-control-allow-origin"] == "https://example.org"
    assert (
        "access-control-allow-origin"
        not in TestClient(create_app(built[0]))
        .get("/healthz", headers={"origin": "https://example.org"})
        .headers
    )


def test_openapi_document_describes_every_route_and_error(client):
    spec = client.get("/openapi.json").json()
    assert {
        "/api/v1/summary",
        "/api/v1/points",
        "/api/v1/variants",
        "/readyz",
        "/healthz",
        "/metrics",
    } <= set(spec["paths"])
    assert {"200", "404", "422", "503"} <= set(spec["paths"]["/api/v1/summary"]["post"]["responses"])
    json.dumps(spec)


# --- differential: the polygon test against an independent method ----------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.tuples(st.floats(-3, 3), st.floats(-3, 3)), min_size=3, max_size=3), st.integers(0, 2**31))
def test_ray_casting_agrees_with_half_planes_on_triangles(tri, seed):
    """A triangle needs no ray casting: a point is inside when it is on the same side of all three edges."""
    rng = np.random.default_rng(seed)
    x, y = rng.uniform(-3, 3, 2000), rng.uniform(-3, 3, 2000)
    (ax, ay), (bx, by), (cx, cy) = tri
    d1 = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
    d2 = (cx - bx) * (y - by) - (cy - by) * (x - bx)
    d3 = (ax - cx) * (y - cy) - (ay - cy) * (x - cx)
    inside = ((d1 > 0) & (d2 > 0) & (d3 > 0)) | ((d1 < 0) & (d2 < 0) & (d3 < 0))
    clear = (
        np.minimum(np.minimum(np.abs(d1), np.abs(d2)), np.abs(d3)) > 1e-9
    )  # ignore points within rounding of an edge
    got = points_in_polygon(x, y, tri)
    assert (got[clear] == inside[clear]).all()
