"""Ingestion of real EVE Online data into the raw store.

Each function fetches, wraps the response in an envelope and writes it to the immutable raw
store. Nothing here parses or cleans: that happens in goldstandard.parse, against raw files,
so a parser fix can be replayed over everything we ever fetched.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from goldstandard.config import Settings, settings
from goldstandard.obs import log, metrics
from goldstandard.raw import RawStore
from goldstandard.sources.esi import EsiClient

logger = logging.getLogger(__name__)
SOURCE = "eve"
PATCH_RSS_URL = "https://www.eveonline.com/rss/patch-notes"


def load_universe(path: Path | None = None) -> dict[str, Any]:
    with (path or settings().reference_dir / "eve_universe.json").open() as fh:
        data: dict[str, Any] = json.load(fh)
    return data


@dataclass
class IngestReport:
    kind: str
    requested: int = 0
    stored: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.failures)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "requested": self.requested,
            "stored": self.stored,
            "failed": len(self.failures),
            "failures": self.failures[:20],
        }


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ingest_history(
    cfg: Settings, store: RawStore, client: EsiClient | None = None, universe: dict[str, Any] | None = None
) -> IngestReport:
    """Fetch the ~13 month daily trade history for every (region, item)."""
    universe = universe or load_universe()
    client = client or EsiClient(cfg)
    report = IngestReport("market_history")
    now = _now()
    jobs = [(s, it["type_id"]) for s in universe["servers"] for it in universe["items"]]
    report.requested = len(jobs)

    def one(job: tuple[dict[str, Any], int]) -> None:
        server, type_id = job
        f = client.market_history(server["region_id"], type_id)
        key = f"{server['server_id']}_{type_id}"
        if not f.ok or f.body is None:
            report.failures.append({"key": key, "status": f.status, "error": f.error})
            return
        store.put(
            source=SOURCE,
            kind="market_history",
            day=now.date(),
            key=key,
            body=f.body,
            fetched_at=now,
            observed_at=now,
            meta={
                "server_id": server["server_id"],
                "region_id": server["region_id"],
                "type_id": type_id,
                "url": f.url,
                "last_modified": f.headers.get("last-modified"),
            },
        )
        report.stored += 1

    with ThreadPoolExecutor(cfg.esi_concurrency) as pool:
        list(pool.map(one, jobs))
    metrics.inc("ingest_payloads", report.stored, source=SOURCE, kind=report.kind)
    log(logger, logging.INFO, "history ingest done", **report.as_dict())
    return report


def ingest_order_snapshot(
    cfg: Settings, store: RawStore, client: EsiClient | None = None, universe: dict[str, Any] | None = None
) -> IngestReport:
    """Snapshot the live order book (sell and buy) for every (region, item).

    One raw bundle per (region, snapshot): a JSON list of responses, each page body kept verbatim.
    A failed item is recorded in the bundle with its status and no body, so the parser can tell
    "no orders" (empty list) apart from "we do not know" (fetch failed).
    """
    universe = universe or load_universe()
    client = client or EsiClient(cfg)
    report = IngestReport("orders")
    now = _now()
    report.requested = len(universe["servers"]) * len(universe["items"])
    for server in universe["servers"]:
        with ThreadPoolExecutor(cfg.esi_concurrency) as pool:
            results = list(
                pool.map(
                    lambda it, region=server["region_id"]: (it["type_id"], client.market_orders(region, it["type_id"])),
                    universe["items"],
                )
            )
        responses = []
        for type_id, pages in results:
            for n, page in enumerate(pages, start=1):
                responses.append({"type_id": type_id, "page": n, "status": page.status, "body": page.body})
            if all(p.ok for p in pages):
                report.stored += 1
            else:
                bad = next(p for p in pages if not p.ok)
                report.failures.append(
                    {"key": f"{server['server_id']}_{type_id}", "status": bad.status, "error": bad.error}
                )
        store.put(
            source=SOURCE,
            kind="orders",
            day=now.date(),
            key=f"{server['server_id']}_{now:%H%M}",
            body=json.dumps(responses),
            fetched_at=now,
            observed_at=now,
            meta={"server_id": server["server_id"], "region_id": server["region_id"]},
        )
    metrics.inc("ingest_payloads", report.stored, source=SOURCE, kind=report.kind)
    log(logger, logging.INFO, "order snapshot done", **report.as_dict())
    return report


def ingest_type_metadata(
    cfg: Settings, store: RawStore, client: EsiClient | None = None, universe: dict[str, Any] | None = None
) -> IngestReport:
    universe = universe or load_universe()
    client = client or EsiClient(cfg)
    report = IngestReport("type_meta")
    now = _now()
    report.requested = len(universe["items"])
    group_cache: dict[int, Any] = {}
    for it in universe["items"]:
        t = client.type_info(it["type_id"])
        if not t.ok or t.body is None:
            report.failures.append({"key": str(it["type_id"]), "status": t.status, "error": t.error})
            continue
        tinfo = json.loads(t.body)
        gid = tinfo.get("group_id")
        if gid is not None and gid not in group_cache:
            g = client.group_info(gid)
            ginfo = json.loads(g.body) if g.ok and g.body else {}
            c = client.category_info(ginfo["category_id"]) if "category_id" in ginfo else None
            group_cache[gid] = {"group": ginfo, "category": json.loads(c.body) if c and c.ok and c.body else {}}
        body = json.dumps({"type": tinfo, **group_cache.get(gid, {})})
        store.put(
            source=SOURCE,
            kind="type_meta",
            day=now.date(),
            key=f"type_{it['type_id']}",
            body=body,
            fetched_at=now,
            observed_at=now,
            meta={"type_id": it["type_id"]},
        )
        report.stored += 1
    log(logger, logging.INFO, "type metadata ingest done", **report.as_dict())
    return report


def ingest_patch_notes(cfg: Settings, store: RawStore, transport: httpx.BaseTransport | None = None) -> IngestReport:
    report = IngestReport("patch_rss", requested=1)
    now = _now()
    client = EsiClient(cfg, transport=transport)  # same retry/backoff policy, different host
    f = client.get(PATCH_RSS_URL)
    client.close()
    body, err = f.body, f.error
    if body is None:
        report.failures.append({"key": "patch_rss", "error": err})
        log(logger, logging.ERROR, "patch notes unavailable; previous snapshot stays authoritative", error=err)
        return report
    store.put(
        source=SOURCE,
        kind="patch_rss",
        day=now.date(),
        key="patch_rss",
        body=body,
        fetched_at=now,
        observed_at=now,
        meta={"url": PATCH_RSS_URL},
    )
    report.stored = 1
    return report
