"""Stage 1: acquire raw input and keep it immutable.

Two sources produce the same thing, a GloVe-format text file (`token f1 .. fD` per line):

* `glove-twitter-50`: 1,193,514 tokens from 2B tweets, 50-d. Only that one member of the 1.5 GB
  archive is downloaded, by reading the zip's central directory over HTTP range requests.
* `synth-<rows>`: a seeded generator with the same shape and the same defects, for CI and for
  when the real source is unreachable.

Raw files are written once, under raw/<source>/, with a SOURCE.json recording url and sha256.
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

import numpy as np

from .config import Settings, event, read_json, write_json

GLOVE = "glove-twitter-50"
GLOVE_MEMBER = "glove.twitter.27B.50d.txt"
RETRIES = 5


class HttpRangeFile(io.RawIOBase):
    """Read-only seekable file over HTTP range requests, with capped exponential backoff."""

    def __init__(self, url: str):
        self.url, self.pos = url, 0
        with self._open(None) as r:
            self.size = int(r.headers["Content-Length"])
            if r.headers.get("Accept-Ranges", "").lower() != "bytes":
                raise OSError(f"{url} does not accept range requests")

    def _open(self, rng: tuple[int, int] | None):
        req = urllib.request.Request(self.url, method="GET" if rng else "HEAD")
        if rng:
            req.add_header("Range", f"bytes={rng[0]}-{rng[1]}")
        for attempt in range(RETRIES):
            try:
                return urllib.request.urlopen(req, timeout=60)  # noqa: S310 (https urls from config)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                status = getattr(e, "code", None)
                if status is not None and status < 500 and status != 429:
                    raise  # a 4xx will not get better by asking again
                if attempt == RETRIES - 1:
                    raise
                delay = min(30.0, 2.0**attempt)
                event(
                    "fetch retry",
                    logging.WARNING,
                    url=self.url,
                    attempt=attempt + 1,
                    delay_s=delay,
                    error=str(e),
                )
                time.sleep(delay)
        raise AssertionError("unreachable")

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset + (0, self.pos, self.size)[whence]
        return self.pos

    def readinto(self, b) -> int:  # type: ignore[override]
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        for attempt in range(RETRIES):
            try:
                with self._open((self.pos, self.pos + n - 1)) as r:
                    data = r.read()
                break
            except (TimeoutError, ConnectionError, OSError) as e:  # body died mid-read
                if attempt == RETRIES - 1:
                    raise
                event("fetch retry (body)", logging.WARNING, attempt=attempt + 1, error=str(e))
                time.sleep(min(30.0, 2.0**attempt))
        b[: len(data)] = data
        self.pos += len(data)
        return len(data)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


def _done(raw: Path) -> bool:
    """A source is complete when SOURCE.json exists and every file it lists still hashes the same size."""
    meta = raw / "SOURCE.json"
    if not meta.exists():
        return False
    return all(
        (raw / f["name"]).exists() and (raw / f["name"]).stat().st_size == f["bytes"]
        for f in read_json(meta)["files"]
    )


def fetch_glove(s: Settings) -> str:
    raw = s.raw(GLOVE)
    if _done(raw):
        event("fetch skipped, raw already present", source=GLOVE)
        return GLOVE
    raw.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for url in s.glove_urls:
        try:
            t0 = time.perf_counter()
            remote = HttpRangeFile(url)
            # 8 MB buffer: the member is read front to back in ~25 requests, not 50,000 4 KB ones.
            with zipfile.ZipFile(io.BufferedReader(remote, buffer_size=8 << 20)) as zf:
                info = zf.getinfo(GLOVE_MEMBER)
                event(
                    "fetching",
                    url=url,
                    member=GLOVE_MEMBER,
                    compressed_mb=info.compress_size >> 20,
                    archive_mb=remote.size >> 20,
                )
                tmp = raw / (GLOVE_MEMBER + ".part")
                with zf.open(info) as src, tmp.open("wb") as dst:  # zipfile verifies the CRC at EOF
                    while chunk := src.read(1 << 22):
                        dst.write(chunk)
            tmp.replace(raw / GLOVE_MEMBER)
            secs = time.perf_counter() - t0
            write_json(
                raw / "SOURCE.json",
                {
                    "source": GLOVE,
                    "url": url,
                    "member": GLOVE_MEMBER,
                    "licence": "PDDL 1.0 (Pennington, Socher, Manning 2014)",
                    "dim": 50,
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "seconds": round(secs, 1),
                    "files": [
                        {"name": GLOVE_MEMBER, "bytes": info.file_size, "sha256": _sha256(raw / GLOVE_MEMBER)}
                    ],
                },
            )
            event(
                "fetched",
                source=GLOVE,
                seconds=round(secs, 1),
                mb_per_s=round(info.compress_size / 2**20 / secs, 1),
            )
            return GLOVE
        except (OSError, zipfile.BadZipFile, zlib.error, KeyError) as e:
            last = e
            event("fetch failed", logging.ERROR, url=url, error=repr(e))
    raise RuntimeError(f"could not fetch {GLOVE} from any configured url") from last


# --- generator -------------------------------------------------------------------------------

_ALPHABETS = {
    "latin": "abcdefghijklmnopqrstuvwxyz",
    "cyrillic": "абвгдежзийклмнопрстуфхцчшщыэюя",
    "arabic": "ابتثجحخدذرزسشصضطظعغفقكلمنهوي",
    "kana": "あいうえおかきくけこさしすせそたちつてとなにぬねの",
    "hangul": "가나다라마바사아자차카타파하고노도로모보소오조초",
    "thai": "กขคงจฉชซญดตถทธนบปผฝพฟภมยรลวศสหอ",
}
# Share of tokens per script, roughly what the Twitter vocabulary looks like.
_SCRIPT_P = [0.72, 0.06, 0.08, 0.06, 0.05, 0.03]
DEFECTS = (
    "duplicate_token",
    "wrong_field_count",
    "bad_number",
    "non_finite",
    "bad_encoding",
    "zero_vector",
    "empty_token",
)


def write_synth(
    path: Path, rows: int, seed: int, dim: int = 50, n_clusters: int = 40, start: int = 0
) -> dict[str, int]:
    """Write `rows` lines in GloVe text format, a known number of them defective. Returns planted counts.

    Vectors are a Gaussian mixture whose clusters each have a dominant script, so neighbourhood
    purity means something. Tokens are unique (`start` offsets the counter so a second file can be
    added later without collisions) except for the planted duplicates.
    """
    rng = np.random.default_rng(seed)
    centres = rng.normal(0, 1.0, (n_clusters, dim)).astype(np.float32)
    cluster_script = rng.choice(len(_SCRIPT_P), n_clusters, p=_SCRIPT_P)
    cluster_script[: len(_SCRIPT_P)] = np.arange(len(_SCRIPT_P))  # every script exists
    weights = rng.dirichlet(np.full(n_clusters, 0.6))
    cluster = rng.choice(n_clusters, rows, p=weights)
    spread = rng.uniform(0.25, 0.6, n_clusters).astype(np.float32)
    vec = centres[cluster] + rng.normal(0, 1, (rows, dim)).astype(np.float32) * spread[cluster, None]
    # 90% of a cluster's tokens use its script; the rest are noise from any script.
    script = np.where(
        rng.random(rows) < 0.9, cluster_script[cluster], rng.choice(len(_SCRIPT_P), rows, p=_SCRIPT_P)
    )
    kind = rng.choice(5, rows, p=[0.82, 0.08, 0.05, 0.03, 0.02])  # word, hashtag, number, symbol, placeholder
    length = rng.integers(2, 9, rows)
    letters = rng.integers(0, 1 << 30, (rows, 8))
    defect = rng.choice(len(DEFECTS) + 1, rows, p=[0.993] + [0.001] * len(DEFECTS)) - 1  # -1 = clean
    defect[:2] = -1  # the first rows are clean so there is something to duplicate
    names = list(_ALPHABETS)
    planted = dict.fromkeys(DEFECTS, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    seen_first: str | None = None
    with tmp.open("wb") as f:
        for i in range(rows):
            alpha = _ALPHABETS[names[script[i]]]
            stem = "".join(alpha[v % len(alpha)] for v in letters[i, : length[i]])
            n = start + i
            if kind[i] == 0:
                token = f"{stem}{n:x}"  # hex suffix keeps tokens unique; some end up mixed alphanumeric
            elif kind[i] == 1:
                token = f"#{stem}{n:x}"
            elif kind[i] == 2:
                token = f"{n}"
            elif kind[i] == 3:
                token = "!?*&%$~^"[n % 8] * 2 + f"·{n}"
            else:
                token = f"<tok{n}>"
            if seen_first is None:
                seen_first = token
            nums = " ".join(f"{v:.5f}" for v in vec[i])
            d = defect[i]
            if d >= 0:
                planted[DEFECTS[d]] += 1
            if d == 0:
                token = seen_first
            elif d == 1:
                nums = nums.rsplit(" ", 3)[0]
            elif d == 2:
                nums = nums.replace(" ", " x", 1)
            elif d == 3:
                nums = "inf " + nums.split(" ", 1)[1]
            elif d == 5:
                nums = " ".join(["0.00000"] * dim)
            elif d == 6:
                token = ""
            line = f"{token} {nums}\n".encode()
            if d == 4:
                line = b"\xff\xfe" + line
            f.write(line)
    tmp.replace(path)
    return planted


def fetch_synth(s: Settings) -> str:
    source = f"synth-{s.synth_rows}"
    raw = s.raw(source)
    if _done(raw):
        event("fetch skipped, raw already present", source=source)
        return source
    t0 = time.perf_counter()
    name = "part-0001.txt"
    planted = write_synth(raw / name, s.synth_rows, s.seed)
    write_json(
        raw / "SOURCE.json",
        {
            "source": source,
            "url": None,
            "generator": {"rows": s.synth_rows, "seed": s.seed},
            "dim": 50,
            "planted_defects": planted,
            "files": [{"name": name, "bytes": (raw / name).stat().st_size, "sha256": _sha256(raw / name)}],
        },
    )
    event(
        "generated",
        source=source,
        rows=s.synth_rows,
        seconds=round(time.perf_counter() - t0, 1),
        planted=planted,
    )
    return source


def run(s: Settings) -> str:
    """Resolve DOTS_SOURCE to a concrete source, fetch it, and remember the answer for later stages."""
    if s.source == "synth":
        source = fetch_synth(s)
    elif s.source == GLOVE:
        source = fetch_glove(s)
    elif s.source == "auto":
        try:
            source = fetch_glove(s)
        except RuntimeError as e:
            # Degrade loudly: the manifest carries the source name and the UI shows it.
            event("real source unavailable, falling back to the generator", logging.WARNING, error=str(e))
            source = fetch_synth(s)
    else:
        raise SystemExit(f"DOTS_SOURCE must be auto, synth or {GLOVE}, not {s.source!r}")
    s.data_dir.mkdir(parents=True, exist_ok=True)
    (s.data_dir / "source.txt").write_text(source, encoding="utf-8")
    return source


def current_source(s: Settings) -> str:
    """The source later stages work on: an explicit DOTS_SOURCE, else whatever `fetch` resolved."""
    if s.source == "synth":
        return f"synth-{s.synth_rows}"
    if s.source != "auto":
        return s.source
    marker = s.data_dir / "source.txt"
    if not marker.exists():
        raise SystemExit("no source fetched yet: run `python -m dots fetch` first")
    return marker.read_text(encoding="utf-8").strip()
