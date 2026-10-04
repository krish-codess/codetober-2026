"""EVE Online ESI client: public market + universe endpoints, no credentials required.

Failure policy (documented in docs/OPERATIONS.md):
- 5xx, 420 (error-limited), 429, timeouts and connection errors are retried with full-jitter
  exponential backoff, capped per attempt (http_backoff_cap_s) and in count (http_max_retries).
- Retry-After and the X-Esi-Error-Limit-* headers are honoured so we back off before ESI bans us.
- 4xx other than the above are permanent: returned as a failure, never retried.
- A failed (region, item) fetch never aborts a run; the caller records it and downstream sees a
  missing observation (thin market), which the index handles without fabricating a price.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from goldstandard.config import Settings
from goldstandard.obs import log, metrics

logger = logging.getLogger(__name__)
RETRYABLE = {420, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    body: str | None
    headers: dict[str, str]
    attempts: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.body is not None


class EsiClient:
    def __init__(self, cfg: Settings, transport: httpx.BaseTransport | None = None, sleep: Any = time.sleep) -> None:
        self.cfg = cfg
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=cfg.esi_base_url,
            timeout=cfg.http_timeout_s,
            headers={"User-Agent": cfg.esi_user_agent, "Accept": "application/json"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), self.cfg.http_backoff_cap_s)
        return random.uniform(0, min(self.cfg.http_backoff_cap_s, self.cfg.http_backoff_base_s * 2**attempt))

    def get(self, path: str, params: dict[str, Any] | None = None) -> Fetched:
        if not path.startswith("http"):  # absolute URLs (e.g. the patch RSS) skip ESI params
            params = {"datasource": "tranquility", **(params or {})}
        last_err = "unknown"
        status = 0
        endpoint = "external" if path.startswith("http") else path.split("/")[1]
        for attempt in range(self.cfg.http_max_retries + 1):
            try:
                with metrics.timer("esi_request", endpoint=endpoint):
                    r = self._client.get(path, params=params)
                status = r.status_code
                remain = r.headers.get("x-esi-error-limit-remain")
                if remain is not None and remain.isdigit() and int(remain) < 10:
                    reset = r.headers.get("x-esi-error-limit-reset", "10")
                    log(logger, logging.WARNING, "esi error budget low, pausing", remain=remain, reset=reset)
                    self._sleep(min(float(reset) if reset.isdigit() else 10.0, self.cfg.http_backoff_cap_s))
                if r.status_code == 200:
                    metrics.inc("esi_requests", status="200")
                    return Fetched(str(r.url), 200, r.text, dict(r.headers), attempt + 1)
                metrics.inc("esi_requests", status=str(r.status_code))
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code not in RETRYABLE:
                    return Fetched(str(r.url), r.status_code, None, dict(r.headers), attempt + 1, last_err)
                wait = self._backoff(attempt, r.headers.get("retry-after"))
            except httpx.TransportError as exc:
                metrics.inc("esi_requests", status="transport_error")
                last_err = f"{type(exc).__name__}: {exc}"
                wait = self._backoff(attempt, None)
            if attempt < self.cfg.http_max_retries:
                log(
                    logger,
                    logging.INFO,
                    "esi retry",
                    path=path,
                    attempt=attempt + 1,
                    wait_s=round(wait, 2),
                    error=last_err,
                )
                self._sleep(wait)
        log(logger, logging.ERROR, "esi request gave up", path=path, params=params, error=last_err)
        return Fetched(path, status, None, {}, self.cfg.http_max_retries + 1, last_err)

    def get_paged(self, path: str, params: dict[str, Any] | None = None) -> list[Fetched]:
        first = self.get(path, {**(params or {}), "page": 1})
        pages = [first]
        if first.ok:
            n = int(first.headers.get("x-pages", "1") or 1)
            pages += [self.get(path, {**(params or {}), "page": p}) for p in range(2, min(n, 50) + 1)]
        return pages

    # --- endpoint helpers -------------------------------------------------------------------
    def market_history(self, region_id: int, type_id: int) -> Fetched:
        return self.get(f"/markets/{region_id}/history/", {"type_id": type_id})

    def market_orders(self, region_id: int, type_id: int) -> list[Fetched]:
        return self.get_paged(f"/markets/{region_id}/orders/", {"type_id": type_id, "order_type": "all"})

    def type_info(self, type_id: int) -> Fetched:
        return self.get(f"/universe/types/{type_id}/")

    def group_info(self, group_id: int) -> Fetched:
        return self.get(f"/universe/groups/{group_id}/")

    def category_info(self, category_id: int) -> Fetched:
        return self.get(f"/universe/categories/{category_id}/")
