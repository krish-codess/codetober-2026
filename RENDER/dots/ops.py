"""Operations on a published site: assemble it for a CDN, check it from outside, roll it back.

A site is a directory of static files:

    index.html, assets/        the built page (content-hashed asset names)
    data/manifest.json         the only mutable file: names the current build
    data/manifest.previous.json
    data/<build_id>/           tiles, density grids and meta.parquet; never modified once written

Switching builds is therefore one small file changing, and rolling back is changing it back.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import event, read_json, write_json
from .tiles import decode_tile

BUILD_DIR = re.compile(r"[0-9a-f]{12}")


def remember_previous(data: Path, new_build: str) -> None:
    """Called before manifest.json is replaced: keep the outgoing manifest so the switch can be undone."""
    current = data / "manifest.json"
    if current.exists() and read_json(current)["build_id"] != new_build:
        shutil.copyfile(current, data / "manifest.previous.json")


def rollback(site: Path) -> str:
    """Swap manifest.json with manifest.previous.json. Running it twice rolls forward again."""
    data = site / "data"
    current, previous = data / "manifest.json", data / "manifest.previous.json"
    if not previous.exists():
        raise SystemExit("nothing to roll back to: there is no data/manifest.previous.json")
    target = read_json(previous)
    if not (data / target["build_id"] / "DONE").exists():
        raise SystemExit(f"cannot roll back: the files of build {target['build_id']} are no longer on disk")
    was = read_json(current)
    write_json(current, target)
    write_json(previous, was)
    event("rolled back", now=target["build_id"], was=was["build_id"])
    return target["build_id"]


def publish(site: Path, web_dist: Path, target: Path) -> dict:
    """Assemble the static site in `target`, ready to sync to a bucket or CDN origin.

    Order matters when the target is live: immutable files first, the page next, the manifest
    last, so nothing ever points at a file that is not there yet. Safe to re-run: files that are
    already present with the same size are skipped, and build directories neither manifest
    refers to are removed so the target does not grow without bound.
    """
    data = site / "data"
    manifest = read_json(data / "manifest.json")
    keep = {manifest["build_id"]}
    if (data / "manifest.previous.json").exists():
        keep.add(read_json(data / "manifest.previous.json")["build_id"])
    if not (web_dist / "index.html").exists():
        raise SystemExit(f"{web_dist} has no index.html: build the page first (cd web && npm run build)")
    copied = skipped = 0

    def copy(src: Path, dst: Path, always: bool = False) -> None:
        nonlocal copied, skipped
        if not always and dst.exists() and dst.stat().st_size == src.stat().st_size:
            skipped += 1
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
        copied += 1

    for build in sorted(keep):
        for f in (data / build).rglob("*"):
            if f.is_file():
                copy(f, target / "data" / f.relative_to(data))
    for f in (web_dist / "assets").rglob("*"):
        if f.is_file():
            copy(f, target / "assets" / f.relative_to(web_dist / "assets"))
    for f in web_dist.iterdir():
        if f.is_file():
            copy(f, target / f.name, always=True)
    for name in ("manifest.previous.json", "manifest.json"):
        if (data / name).exists():
            copy(data / name, target / "data" / name, always=True)
    pruned = []
    for d in (target / "data").iterdir():
        if d.is_dir() and BUILD_DIR.fullmatch(d.name) and d.name not in keep:
            shutil.rmtree(d)
            pruned.append(d.name)
    out = {
        "build_id": manifest["build_id"],
        "copied": copied,
        "skipped": skipped,
        "pruned": pruned,
        "target": str(target),
    }
    event("published", **out)
    return out


def _get(url: str, method: str = "GET", attempts: int = 4):
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=30)  # noqa: S310
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            last: Exception = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        if attempt < attempts - 1:
            time.sleep(min(8.0, 0.5 * 2**attempt))
    raise last


def verify(base_url: str) -> dict:
    """Check a deployment from the outside, the way a browser would use it. Raises SystemExit on failure."""
    base = base_url.rstrip("/") + "/"
    problems: list[str] = []
    report: dict = {"url": base, "checks": []}

    def check(name: str, fn) -> None:
        try:
            report["checks"].append({"check": name, "ok": True, "detail": fn()})
        except Exception as e:  # noqa: BLE001 (a verifier reports every kind of failure)
            report["checks"].append({"check": name, "ok": False, "detail": f"{type(e).__name__}: {e}"})
            problems.append(name)

    def page() -> str:
        with _get(base) as r:
            html = r.read().decode("utf-8")
        if "A Million Dots" not in html:
            raise ValueError("index.html is not this application")
        asset = re.search(r'src="\.?/?(assets/[^"]+\.js)"', html)
        if not asset:
            raise ValueError("index.html references no script")
        with _get(base + asset[1], "HEAD") as r:
            return f"index.html and {asset[1]} ({r.headers.get('Content-Length')} bytes)"

    manifest: dict = {}

    def load_manifest() -> str:
        with _get(base + "data/manifest.json") as r:
            manifest.update(json.loads(r.read()))
            cache = r.headers.get("Cache-Control", "(none)")
        return f"build {manifest['build_id']}, {manifest['n_points']} points, cache-control: {cache}"

    check("page", page)
    check("manifest", load_manifest)
    for v in manifest.get("variants", []):

        def tiles(v=v) -> str:
            total = 0
            for z, x, y, n in v["tiles"][:3]:  # the first tiles any visitor downloads
                with _get(f"{base}data/{v['tiles_path']}/{z}-{x}-{y}.bin") as r:
                    ids, _, _ = decode_tile(r.read())
                if len(ids) != n:
                    raise ValueError(f"tile {z}-{x}-{y} has {len(ids)} points, manifest says {n}")
                total += n
            with _get(f"{base}data/{v['tiles_path']}/density.u32", "HEAD") as r:
                if int(r.headers["Content-Length"]) != v["density_grid"] ** 2 * 4:
                    raise ValueError("density grid has the wrong size")
            return f"{min(3, len(v['tiles']))} tiles decoded, {total} points"

        check(f"tiles:{v['name']}", tiles)
    if manifest:

        def meta() -> str:
            with _get(base + "data/" + manifest["meta"]["path"], "HEAD") as r:
                size = int(r.headers["Content-Length"])
            if size != manifest["meta"]["bytes"]:
                raise ValueError(f"meta.parquet is {size} bytes, manifest says {manifest['meta']['bytes']}")
            return f"{size} bytes"

        check("metadata", meta)
    report["ok"] = not problems
    event(
        "verified" if not problems else "verification failed",
        logging.INFO if not problems else logging.ERROR,
        url=base,
        failed=problems,
    )
    return report
