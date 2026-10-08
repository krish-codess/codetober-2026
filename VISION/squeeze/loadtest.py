"""Load test for the API. Standard library only, so the numbers in the README can be reproduced
anywhere: `python -m squeeze loadtest --url http://127.0.0.1:8000`."""

from __future__ import annotations

import json
import statistics
import threading
import time
import urllib.error
import urllib.request
from typing import Any


def _call(url: str, body: bytes | None, token: str, etag: str | None = None) -> tuple[int, float]:
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}"}
    if etag:
        headers["if-none-match"] = etag
    request = urllib.request.Request(url, data=body, headers=headers)  # noqa: S310
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    return status, (time.perf_counter() - start) * 1000


def run(url: str, token: str, seconds: float, concurrency: int, payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Each worker loops: nine reads of the tradeoff (the explorer's critical path) to one upload."""
    samples: dict[str, list[float]] = {"GET /v1/tradeoff": [], "POST /v1/results": []}
    statuses: dict[int, int] = {}
    lock = threading.Lock()
    stop = time.perf_counter() + seconds
    queue = iter(payloads)

    def worker() -> None:
        i = 0
        while time.perf_counter() < stop:
            i += 1
            if i % 10 == 0:
                with lock:
                    payload = next(queue, None)
                if payload is None:
                    continue
                name, (status, ms) = "POST /v1/results", _call(f"{url}/v1/results", json.dumps(payload).encode(), token)
            else:
                name = "GET /v1/tradeoff"
                status, ms = _call(f"{url}/v1/tradeoff?target=rpi5&synthetic=true", None, token)
            with lock:
                samples[name].append(ms)
                statuses[status] = statuses.get(status, 0) + 1

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out: dict[str, Any] = {"seconds": seconds, "concurrency": concurrency, "statuses": statuses, "endpoints": {}}
    for name, ms in samples.items():
        if len(ms) >= 2:
            q = statistics.quantiles(ms, n=100)
            out["endpoints"][name] = {
                "requests": len(ms),
                "rps": round(len(ms) / seconds, 1),
                "p50_ms": round(q[49], 2),
                "p95_ms": round(q[94], 2),
                "p99_ms": round(q[98], 2),
            }
    return out
