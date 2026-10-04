"""Download pinned upstream files (dataset + embedding model) with capped exponential backoff.

Hugging Face is the only external dependency of the system and only at seed/build time. Failure
behaviour: retry transient errors up to `attempts` times, then raise FetchError with the URL and the
last cause so the caller can degrade (the seed falls back to the synthetic generator).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .config import EMBED_FILE, EMBED_REPO, EMBED_REVISION, MABSA_REPO, MABSA_REVISION
from .log import event

logger = logging.getLogger(__name__)

DOMAINS = ("coursera", "food", "hotel", "laptop", "phone", "restaurant", "sight")
SPLITS = ("train", "dev", "test")
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(
    url: str,
    dest: Path,
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    client: httpx.Client | None = None,
) -> Path:
    """Fetch `url` to `dest` atomically. Idempotent: an existing file is never re-downloaded."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".part{os.getpid()}")
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))
    last: Exception | None = None
    try:
        for attempt in range(1, attempts + 1):
            try:
                with client.stream("GET", url) as r:
                    if r.status_code in RETRYABLE:
                        raise httpx.TransportError(f"HTTP {r.status_code}")
                    if r.status_code != 200:  # 401/403/404: retrying will not help
                        raise FetchError(f"GET {url} -> HTTP {r.status_code} (not retryable)")
                    with tmp.open("wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                tmp.replace(dest)
                return dest
            except (httpx.TransportError, httpx.StreamError) as e:
                last = e
                tmp.unlink(missing_ok=True)
                if attempt < attempts:
                    delay = min(max_delay, base_delay * 2 ** (attempt - 1))
                    event(logger, "fetch_retry", logging.WARNING, url=url, attempt=attempt, delay_s=delay, error=str(e))
                    sleep(delay)
        raise FetchError(f"GET {url} failed after {attempts} attempts: {last}")
    finally:
        tmp.unlink(missing_ok=True)
        if own:
            client.close()


def mabsa_path(data_dir: Path, domain: str, lang: str, split: str) -> Path:
    return data_dir / "raw" / "mabsa" / MABSA_REVISION[:8] / domain / lang / f"{split}.txt"


def fetch_mabsa(data_dir: Path, langs: Iterable[str]) -> list[Path]:
    base = f"https://huggingface.co/datasets/{MABSA_REPO}/resolve/{MABSA_REVISION}"
    jobs = [
        (f"{base}/{d}/{lang}/{s}.txt", mabsa_path(data_dir, d, lang, s))
        for d in DOMAINS
        for lang in sorted(set(langs))
        for s in SPLITS
    ]
    timeout = httpx.Timeout(30.0, read=120.0)
    with httpx.Client(follow_redirects=True, timeout=timeout) as client, ThreadPoolExecutor(8) as pool:
        return list(pool.map(lambda j: download(j[0], j[1], client=client), jobs))


def fetch_embed_model(data_dir: Path) -> tuple[Path, Path]:
    base = f"https://huggingface.co/{EMBED_REPO}/resolve/{EMBED_REVISION}"
    root = data_dir / "models" / EMBED_REVISION[:8]
    tok = download(f"{base}/tokenizer.json", root / "tokenizer.json")
    model = download(f"{base}/{EMBED_FILE}", root / Path(EMBED_FILE).name)
    return model, tok
