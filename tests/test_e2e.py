"""End to end in a real browser: a recorded run -> failure list -> minimal dataset ->
catalog. Also captures the README screenshots when SCREENSHOT_DIR is set.

Uses Playwright's bundled Chromium, or an installed browser via E2E_BROWSER_CHANNEL
(for example `msedge` or `chrome`)."""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.e2e]
sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def base_url(db: Any, pg_dsn: str, report: dict[str, Any],
             monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    import uvicorn

    from tydlc import api, store

    run, _ = store.create_run(db, "e2e", "jaffle", "duckdb", 28, 40)
    store.finish_run(db, run["id"], report)
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api.create_app(), host="127.0.0.1", port=port,
                                           log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel=os.environ.get("E2E_BROWSER_CHANNEL") or None)
        except sync_api.Error as exc:
            pytest.skip(f"no browser available: {str(exc).splitlines()[0]}")
        yield b
        b.close()


def _shot(page: Any, name: str) -> None:
    if folder := os.environ.get("SCREENSHOT_DIR"):
        page.screenshot(path=str(Path(folder) / name), full_page=True)


def test_primary_journey(browser: Any, base_url: str) -> None:
    expect = sync_api.expect
    page = browser.new_page(viewport={"width": 1200, "height": 850})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base_url)

    # Failure list for the latest run.
    expect(page.get_by_role("heading", level=1)).to_contain_text("Failures in run")
    expect(page.get_by_text("CI gate")).to_be_visible()
    link = page.get_by_role("link").filter(has_text="orders_amount_not_null").first
    expect(link).to_contain_text("known bug")
    expect(link).to_contain_text("minimal case 2 rows")
    _shot(page, "1-failures.png")

    # Keyboard only: focus the failure, open it, land on its heading.
    link.focus()
    page.keyboard.press("Enter")
    heading = page.get_by_role("heading", level=1)
    expect(heading).to_have_text("orders_amount_not_null")
    expect(heading).to_be_focused()
    expect(page.get_by_text("an order with no payments")).to_be_visible()
    tables = page.locator("main table")
    expect(tables).to_have_count(2)  # raw_customers and raw_orders; raw_payments is empty
    expect(tables.nth(1)).to_contain_text('"placed"')
    expect(page.locator(".null").first).to_have_text("NULL")
    expect(page.get_by_text("No rows are needed to reproduce")).to_be_visible()
    expect(page.locator("pre").first).to_have_text(
        "tydlc run --engine duckdb --seed 28 --max-examples 40")
    _shot(page, "2-minimal-dataset.png")

    # Catalog: confidence per invariant, filterable.
    page.get_by_role("link", name="Invariant catalog").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("Invariant catalog")
    rows = page.locator("main tbody tr")
    expect(rows.first).to_contain_text("Falsified")  # falsified sort first
    total = rows.count()
    assert total > 50
    _shot(page, "3-catalog.png")
    search = page.get_by_role("searchbox", name="Filter invariants")
    search.fill("row_count_eq")
    expect(rows.first).to_contain_text("row_count_eq")
    expect(rows.first).to_contain_text("Held")
    expect(search).to_be_focused()  # typing must not lose focus on re-render
    assert 0 < rows.count() < total
    search.fill("zzz-no-such-invariant")
    expect(page.get_by_text("No invariant matches these filters.")).to_be_visible()

    # Phone width: no horizontal page scroll, navigation still usable.
    page.set_viewport_size({"width": 375, "height": 740})
    search.fill("orders.amount")
    expect(rows.first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    _shot(page, "4-catalog-phone.png")
    page.get_by_role("link", name="Failures").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("Failures in run")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []


def test_error_and_empty_states(browser: Any, base_url: str, db: Any) -> None:
    expect = sync_api.expect
    page = browser.new_page()
    page.route("**/api/runs?*", lambda route: route.fulfill(
        status=503, content_type="application/json",
        body='{"error":{"code":"database_unavailable","message":"the results database is '
             'unreachable; retry shortly","correlation_id":"abc123"}}'))
    page.goto(base_url)
    alert = page.get_by_role("alert")
    expect(alert).to_contain_text("the results database is unreachable")
    expect(alert).to_contain_text("abc123")

    page.unroute("**/api/runs?*")
    db.execute("TRUNCATE runs CASCADE")
    page.get_by_role("button", name="Try again").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("No runs yet")
