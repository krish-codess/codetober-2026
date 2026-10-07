"""The API against a real PostgreSQL: every route's success, validation, authorization and malformed-input paths,
plus the properties that matter most: nobody can read anyone else's story, retries are safe, and a
dead database produces an answer a client can act on.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from wrapped import auth
from wrapped.api import create_app
from wrapped.config import Settings
from wrapped.publish import publish, rollback

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"Authorization": "Bearer admin-token-for-tests-0123456789"}


@pytest.fixture(scope="module")
def served(pipeline: Settings, database: dict[str, str], tmp_path_factory: pytest.TempPathFactory) -> Settings:
    settings = dataclasses.replace(
        pipeline,
        api_database_url=database["api"],
        batch_database_url=database["batch"],
        migrate_database_url=database["owner"],
        token_secret="test-secret-0123456789abcdef",
        admin_token="admin-token-for-tests-0123456789",
        public_base_url="https://wrapped.example",
    )
    result = publish(settings)
    assert result["activated"] and result["loaded"] > 0
    return settings


@pytest.fixture(scope="module")
def client(served: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(served), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def users(served: Settings) -> list[dict]:
    """Two full-tier users and a minimal one, straight from the database the API reads."""
    with psycopg.connect(served.migrate_database_url) as conn:
        rows = conn.execute(
            """(SELECT user_id, login, tier FROM wrapped_payloads WHERE tier = 'full' ORDER BY user_id LIMIT 2)
               UNION ALL (SELECT user_id, login, tier FROM wrapped_payloads WHERE tier = 'minimal' ORDER BY user_id LIMIT 1)"""
        ).fetchall()
    return [{"id": r[0], "login": r[1], "tier": r[2], "auth": bearer(served, r[0])} for r in rows]


def bearer(settings: Settings, user_id: int, **kw: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.mint(settings.token_secret, user_id, settings.year, **kw)}"}  # type: ignore[arg-type]


def assert_error(response, status: int, code: str) -> None:  # noqa: ANN001
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code and error["message"] and error["request_id"] == response.headers["x-request-id"]
    assert "Traceback" not in response.text


# ---- the story ----------------------------------------------------------------------------------------------


def test_story_is_the_token_holders_and_only_theirs(client: TestClient, users: list[dict]) -> None:
    a, b = users[0], users[1]
    story_a = client.get("/v1/wrapped", headers=a["auth"])
    story_b = client.get("/v1/wrapped", headers=b["auth"])
    assert story_a.status_code == story_b.status_code == 200
    assert story_a.json()["user"] == {"id": a["id"], "login": a["login"]}
    assert story_b.json()["user"]["id"] == b["id"]
    # Nothing of B anywhere in A's response, by id or by login.
    assert str(b["id"]) not in story_a.text and b["login"] not in story_a.text
    cards = story_a.json()["cards"]
    assert cards[0]["type"] == "intro" and cards[-1]["type"] == "summary"
    assert all("facts" not in c for c in cards), "audit facts are internal; the contract is presentational"
    # A browser must never replay A's story to the next person who opens a link on the same device.
    assert story_a.headers["cache-control"] == "private, no-cache" and story_a.headers["vary"] == "Authorization"


def test_no_route_accepts_a_user_id(client: TestClient, users: list[dict]) -> None:
    a, b = users[0], users[1]
    for attempt in (f"/v1/wrapped?user_id={b['id']}", f"/v1/wrapped?user={b['id']}"):
        assert client.get(attempt, headers=a["auth"]).json()["user"]["id"] == a["id"]
    assert client.get(f"/v1/wrapped/{b['id']}", headers=a["auth"]).status_code == 404


@pytest.mark.parametrize("header", [{}, {"Authorization": "Bearer"}, {"Authorization": "Bearer not.a-token"},
                                    {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "Bearer é.é"}])  # fmt: skip
def test_story_rejects_missing_and_malformed_tokens(client: TestClient, header: dict) -> None:
    assert_error(client.get("/v1/wrapped", headers={k: v.encode() for k, v in header.items()}), 401, "invalid_token")


def test_story_rejects_expired_wrong_year_and_wrong_key_tokens(
    client: TestClient, served: Settings, users: list[dict]
) -> None:
    uid = users[0]["id"]
    assert_error(client.get("/v1/wrapped", headers=bearer(served, uid, ttl_seconds=-10)), 401, "invalid_token")
    wrong_year = {"Authorization": f"Bearer {auth.mint(served.token_secret, uid, served.year - 1)}"}
    assert_error(client.get("/v1/wrapped", headers=wrong_year), 401, "invalid_token")
    wrong_key = {"Authorization": f"Bearer {auth.mint('another-secret-0123456789', uid, served.year)}"}
    assert_error(client.get("/v1/wrapped", headers=wrong_key), 401, "invalid_token")


def test_valid_token_for_an_account_with_no_story_is_a_clear_404(client: TestClient, served: Settings) -> None:
    assert_error(client.get("/v1/wrapped", headers=bearer(served, 999_999_999)), 404, "wrapped_not_found")


def test_story_revalidates_with_etag(client: TestClient, users: list[dict]) -> None:
    first = client.get("/v1/wrapped", headers=users[0]["auth"])
    again = client.get("/v1/wrapped", headers=users[0]["auth"] | {"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    other = client.get("/v1/wrapped", headers=users[1]["auth"] | {"If-None-Match": first.headers["etag"]})
    assert other.status_code == 200, "one user's ETag never validates another user's story"


# ---- views and shares ---------------------------------------------------------------------------------------


def test_view_is_idempotent_and_validated(client: TestClient, served: Settings, users: list[dict]) -> None:
    a = users[0]
    for _ in range(3):
        assert client.put("/v1/wrapped/views/summary", headers=a["auth"]).status_code == 204
    with psycopg.connect(served.migrate_database_url) as conn:
        count = conn.execute(
            "SELECT count(*) FROM card_views WHERE user_id = %s AND card_type = 'summary'", [a["id"]]
        ).fetchone()
    assert count == (1,)
    assert_error(client.put("/v1/wrapped/views/no_such_card", headers=a["auth"]), 404, "card_not_in_story")
    assert_error(
        client.put("/v1/wrapped/views/Robert'); DROP TABLE shares;--", headers=a["auth"]), 422, "invalid_request"
    )
    assert_error(client.put("/v1/wrapped/views/summary"), 401, "invalid_token")


def test_share_is_retry_safe_and_public_view_is_one_card(client: TestClient, users: list[dict]) -> None:
    a, b = users[0], users[1]
    created = client.post("/v1/wrapped/shares", headers=a["auth"], json={"card_type": "summary"})
    assert created.status_code == 201 and created.json()["created"] is True
    retried = client.post("/v1/wrapped/shares", headers=a["auth"], json={"card_type": "summary"})
    assert retried.status_code == 200 and retried.json()["created"] is False
    share = created.json()
    assert retried.json()["share_id"] == share["share_id"] and len(share["share_id"]) >= 22
    assert share["url"] == f"https://wrapped.example/s/{share['share_id']}"

    public = client.get(f"/v1/shares/{share['share_id']}")  # no token
    assert public.status_code == 200
    body = public.json()
    assert set(body) == {"share_id", "login", "year", "card", "image_url"} and body["card"]["type"] == "summary"
    assert "facts" not in body["card"] and str(a["id"]) not in public.text, "no user id, no audit facts, no other cards"

    image = client.get(f"/v1/shares/{share['share_id']}/card.png")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png" and image.content[:4] == b"\x89PNG"
    assert "immutable" in image.headers["cache-control"]

    page = client.get(f"/s/{share['share_id']}")
    assert page.status_code == 200 and 'property="og:image"' in page.text and "summary_large_image" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]

    other = client.post("/v1/wrapped/shares", headers=b["auth"], json={"card_type": "summary"})
    assert other.json()["share_id"] != share["share_id"]


def test_share_validation_and_authorization(client: TestClient, users: list[dict]) -> None:
    a = users[0]
    assert_error(client.post("/v1/wrapped/shares", json={"card_type": "summary"}), 401, "invalid_token")
    assert_error(
        client.post("/v1/wrapped/shares", headers=a["auth"], json={"card_type": "intro"}), 409, "card_not_shareable"
    )
    assert_error(
        client.post("/v1/wrapped/shares", headers=a["auth"], json={"card_type": "no_such_card"}),
        404,
        "card_not_in_story",
    )
    for bad in ({}, {"card_type": 7}, {"card_type": "x" * 200}, {"card_type": "../../etc"}):
        assert_error(client.post("/v1/wrapped/shares", headers=a["auth"], json=bad), 422, "invalid_request")
    garbage = client.post(
        "/v1/wrapped/shares", headers=a["auth"] | {"content-type": "application/json"}, content=b"{not json"
    )
    assert_error(garbage, 422, "invalid_request")
    assert garbage.json()["error"]["details"], "validation errors say which field"


@pytest.mark.parametrize("share_id", ["AAAAAAAAAAAAAAAAAAAAAA", "short", "a" * 300, "..%2F..%2Fetc%2Fpasswd", "';--"])
def test_unknown_and_malformed_share_ids(client: TestClient, share_id: str) -> None:
    for path in (f"/v1/shares/{share_id}", f"/v1/shares/{share_id}/card.png", f"/s/{share_id}"):
        assert client.get(path).status_code == 404


def test_share_page_escapes_what_it_prints(client: TestClient, served: Settings, users: list[dict]) -> None:
    share_id = "x" * 22
    with psycopg.connect(served.migrate_database_url) as conn:
        conn.execute(
            """INSERT INTO shares (share_id, user_id, year, card_type, login, card, source_run_id)
               SELECT %s, 1, 2025, 'first_event', '<script>alert(1)</script>',
                      '{"type":"first_event","family":"origin","shareable":true,"headline":"<img src=x onerror=alert(1)>",
                        "value":"\\"><b>","unit":"","body":"ok","claim":null}'::jsonb, run_id FROM active_runs""",
            [share_id],
        )
    page = client.get(f"/s/{share_id}").text
    assert "<script>alert" not in page and "<img src=x" not in page and "&lt;script&gt;" in page


# ---- admin --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/v1/admin/analytics/superlatives", "/v1/admin/analytics/share-rate", "/v1/admin/payloads"]
)
def test_admin_routes_need_the_admin_token(client: TestClient, users: list[dict], path: str) -> None:
    assert_error(client.get(path), 401, "invalid_token")
    assert_error(client.get(path, headers=users[0]["auth"]), 403, "forbidden")
    assert client.get(path, headers=ADMIN).status_code == 200


def test_superlative_distribution_matches_the_run(client: TestClient, served: Settings) -> None:
    body = client.get("/v1/admin/analytics/superlatives", headers=ADMIN).json()
    by_type = {c["card_type"]: c for c in body["cards"]}
    assert by_type["intro"]["users"] == by_type["summary"]["users"] == body["users"] > 0
    assert all(0 < c["share_of_users"] <= 1 and c["users"] <= body["users"] for c in body["cards"])
    assert len(by_type) >= 15


def test_share_rate_counts_viewers_and_sharers(client: TestClient, users: list[dict]) -> None:
    for u in users:
        client.put("/v1/wrapped/views/summary", headers=u["auth"])
    client.post("/v1/wrapped/shares", headers=users[0]["auth"], json={"card_type": "summary"})
    rows = {r["card_type"]: r for r in client.get("/v1/admin/analytics/share-rate", headers=ADMIN).json()}
    summary = rows["summary"]
    assert summary["viewers"] >= 3 and 1 <= summary["sharers"] <= summary["viewers"]
    assert summary["share_rate"] == round(summary["sharers"] / summary["viewers"], 4)
    assert "intro" not in rows, "cards that cannot be shared have no share rate"
    assert any(r["share_rate"] is None for r in rows.values()), "no views yet is reported as null, not zero"


def test_payload_listing_pages_with_a_stable_cursor(client: TestClient, served: Settings) -> None:
    seen: list[int] = []
    cursor = None
    for _ in range(1000):
        page = client.get(
            "/v1/admin/payloads", headers=ADMIN, params={"limit": 97} | ({"cursor": cursor} if cursor else {})
        ).json()
        seen += [item["user_id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    with psycopg.connect(served.migrate_database_url) as conn:
        total = conn.execute("SELECT count(*) FROM wrapped_payloads p JOIN active_runs a USING (run_id)").fetchone()
    assert total is not None and len(seen) == total[0] and seen == sorted(set(seen)), "every user once, in order"
    for bad in ({"limit": 0}, {"limit": 201}, {"limit": "many"}, {"cursor": "!!!"}, {"cursor": "x" * 100}):
        assert client.get("/v1/admin/payloads", headers=ADMIN, params=bad).status_code == 422


# ---- operations, failure, privilege ------------------------------------------------------------------------


def test_readiness_exercises_the_database_and_card_store(client: TestClient) -> None:
    body = client.get("/readyz").json()
    assert body["status"] == "ready" and body["checks"]["database"] == "ok" and body["checks"]["card_store"] == "ok"
    assert len(body["checks"]["active_run"]) == 36
    metrics = client.get("/metrics").text
    assert 'wrapped_http_requests_total{method="GET",route="/v1/wrapped",status="200"}' in metrics
    assert "wrapped_card_render_seconds_count" in metrics


def test_request_ids_are_echoed_or_generated(client: TestClient) -> None:
    assert client.get("/healthz", headers={"X-Request-ID": "trace-abc.123"}).headers["x-request-id"] == "trace-abc.123"
    generated = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces\tand tabs"}).headers["x-request-id"]
    assert len(generated) == 32
    assert_error(client.get("/no/such/route"), 404, "not_found")
    assert_error(client.delete("/v1/wrapped"), 405, "method_not_allowed")


def test_database_down_is_a_503_the_client_can_retry(served: Settings, users: list[dict]) -> None:
    """Failure injection: the API pointed at a port where nothing listens."""
    broken = dataclasses.replace(served, api_database_url="postgresql://nobody@127.0.0.1:9/nothing")
    with TestClient(create_app(broken), raise_server_exceptions=False) as down:
        assert down.get("/healthz").status_code == 200, "liveness does not depend on the database"
        ready = down.get("/readyz")
        assert ready.status_code == 503 and ready.json()["checks"]["database"].startswith("failed")
        response = down.get("/v1/wrapped", headers=users[0]["auth"])
        assert_error(response, 503, "database_unavailable")
        assert response.headers["retry-after"] == "5"
        assert_error(down.get("/v1/shares/" + "A" * 22), 503, "database_unavailable")
        assert down.get("/metrics").text.count("wrapped_db_unavailable_total 2.0") == 1


def test_api_credentials_cannot_rewrite_a_story_and_batch_cannot_read_behaviour(
    served: Settings, database: dict[str, str]
) -> None:
    with psycopg.connect(database["api"]) as api:
        for forbidden in (
            "UPDATE wrapped_payloads SET payload = '{}'",
            "DELETE FROM wrapped_payloads",
            "UPDATE active_runs SET run_id = run_id",
            "DELETE FROM shares",
            "DELETE FROM card_views",
            "INSERT INTO card_types VALUES ('x', 'y', true)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                api.execute(forbidden)
            api.rollback()
    with psycopg.connect(database["batch"]) as batch:
        for forbidden in ("SELECT * FROM card_views", "SELECT * FROM shares"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                batch.execute(forbidden)
            batch.rollback()


def test_schema_refuses_what_the_application_should_never_send(served: Settings) -> None:
    with psycopg.connect(served.migrate_database_url) as conn:
        run = conn.execute("SELECT run_id FROM active_runs").fetchone()
        assert run is not None
        for bad, error in (
            ("INSERT INTO wrapped_payloads VALUES (%s, 1, 'x', 'huge', '{}')", psycopg.errors.CheckViolation),
            ("INSERT INTO wrapped_payloads VALUES (%s, -5, 'x', 'full', '{}')", psycopg.errors.CheckViolation),
            ("INSERT INTO wrapped_payloads VALUES (%s, 1, 'x', 'full', '[]')", psycopg.errors.CheckViolation),
            ("INSERT INTO payload_cards VALUES (%s, 123456789, 0, 'summary')", psycopg.errors.ForeignKeyViolation),
            ("INSERT INTO card_views VALUES (1, 2025, 'not_a_card_type')", psycopg.errors.ForeignKeyViolation),
            ("UPDATE generation_runs SET finished_at = NULL WHERE run_id = %s", psycopg.errors.CheckViolation),
        ):
            with pytest.raises(error):
                conn.execute(bad, [run[0]] if "%s" in bad else None)
            conn.rollback()


def test_publish_is_idempotent_and_rollback_is_one_step(
    served: Settings, client: TestClient, users: list[dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    again = publish(served)
    assert again["loaded"] == 0 and again["activated"] is False, "re-publishing a loaded run does nothing"
    with pytest.raises(SystemExit, match="nothing to roll back"):
        rollback(served)

    # A second run built from the same warehouse under a different k: a different run id, different payloads on disk.
    second = dataclasses.replace(served, k_anonymity=served.k_anonymity + 1)
    monkeypatch.setattr(Settings, "payload_dir", property(lambda self: tmp_path / "payloads"))
    from wrapped.build import build

    built = build(second)
    result = publish(second)
    assert result["activated"] and result["run_id"] == built["run_id"]
    first_run = again["run_id"]
    assert client.get("/v1/wrapped", headers=users[0]["auth"]).json()["run_id"] == built["run_id"]

    back = rollback(served)
    assert back == {"now_serving": first_run, "rolled_back_from": built["run_id"]}
    assert client.get("/v1/wrapped", headers=users[0]["auth"]).json()["run_id"] == first_run
    assert rollback(served)["now_serving"] == built["run_id"], "rolling back twice returns to the newer run"
    rollback(served)


def test_migrations_roll_back_and_forward(database: dict[str, str]) -> None:
    """Defined last in this module on purpose: its final step empties the schema."""
    env = os.environ | {"WRAPPED_MIGRATE_DATABASE_URL": database["owner"]}

    def alembic(*args: str) -> None:
        subprocess.run([sys.executable, "-m", "alembic", *args], check=True, cwd=ROOT, env=env, capture_output=True)

    def tables() -> set[str]:
        with psycopg.connect(database["owner"]) as conn:
            return {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}

    with psycopg.connect(database["owner"]) as conn:
        before = conn.execute("SELECT count(*) FROM wrapped_payloads").fetchone()
    alembic("downgrade", "0001")
    with psycopg.connect(database["api"]) as api, pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute("SELECT 1 FROM wrapped_payloads LIMIT 1")
    alembic("upgrade", "head")
    with psycopg.connect(database["api"]) as api:
        assert api.execute("SELECT count(*) FROM wrapped_payloads").fetchone() == before, "0002 down/up loses no data"
    assert tables() >= {
        "generation_runs",
        "active_runs",
        "wrapped_payloads",
        "payload_cards",
        "card_types",
        "card_views",
        "shares",
    }

    # And the whole way down and back: 0001 has a real rollback, not a comment saying "restore from backup".
    alembic("downgrade", "base")
    assert tables() <= {"alembic_version"}
    alembic("upgrade", "head")
    assert "wrapped_payloads" in tables()
