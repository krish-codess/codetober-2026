"""Deploy, verify and roll back, rehearsed against a plain static file server (what a CDN is)."""

from __future__ import annotations

import dataclasses
import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dots import ops, tiles
from dots.config import read_json


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture()
def two_builds(site_copy, built):
    """A site that has been published twice: the second build uses a different tile capacity."""
    first = built[2]["build_id"]
    second = tiles.run(dataclasses.replace(site_copy, tile_capacity=128), built[1])["build_id"]
    assert second != first
    return site_copy, first, second


@pytest.fixture()
def web_dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<title>A Million Dots</title><script type="module" src="./assets/index-abc.js"></script>',
        encoding="utf-8",
    )
    (dist / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    return dist


@pytest.fixture()
def cdn(tmp_path):
    target = tmp_path / "cdn"
    target.mkdir()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(target)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield target, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_publish_verify_rollback_and_forward_again(two_builds, web_dist, cdn):
    s, first, second = two_builds
    target, url = cdn
    stale = target / "data" / "0123456789ab"
    stale.mkdir(parents=True)
    (stale / "old.bin").write_bytes(b"x")

    out = ops.publish(s.site_dir, web_dist, target)
    assert out["build_id"] == second and out["pruned"] == ["0123456789ab"] and not stale.exists()
    assert (target / "data" / first / "DONE").exists(), (
        "the previous build stays published so a rollback has something to point at"
    )
    report = ops.verify(url)
    assert report["ok"], report
    assert {c["check"] for c in report["checks"]} >= {"page", "manifest", "metadata", "tiles:pca"}
    assert second in next(c["detail"] for c in report["checks"] if c["check"] == "manifest")

    again = ops.publish(s.site_dir, web_dist, target)
    assert again["copied"] == 3, (
        "a re-publish rewrites only index.html and the two manifests; immutable files are skipped"
    )

    assert ops.rollback(s.site_dir) == first
    ops.publish(s.site_dir, web_dist, target)
    report = ops.verify(url)
    assert report["ok"] and first in next(c["detail"] for c in report["checks"] if c["check"] == "manifest")
    assert read_json(target / "data" / "manifest.previous.json")["build_id"] == second

    assert ops.rollback(s.site_dir) == second, "rolling back twice rolls forward"


def test_verify_catches_a_broken_deployment(two_builds, web_dist, cdn):
    s, _, second = two_builds
    target, url = cdn
    ops.publish(s.site_dir, web_dist, target)
    root = target / "data" / second / "pca" / "0-0-0.bin"
    root.write_bytes(root.read_bytes()[:-5])  # an upload cut short
    (target / "data" / second / "meta.parquet").write_bytes(b"PAR1")
    report = ops.verify(url)
    failed = {c["check"] for c in report["checks"] if not c["ok"]}
    assert not report["ok"] and failed == {"tiles:pca", "metadata"}

    (target / "data" / "manifest.json").unlink()
    assert not ops.verify(url)["ok"]
    assert not ops.verify("http://127.0.0.1:9")["ok"], "an unreachable host is a failed check, not a crash"


def test_rollback_refuses_when_there_is_nothing_safe_to_go_back_to(site_copy, two_builds):
    s, first, _ = two_builds
    import shutil

    shutil.rmtree(s.site_dir / "data" / first)
    with pytest.raises(SystemExit, match="no longer on disk"):
        ops.rollback(s.site_dir)
    (s.site_dir / "data" / "manifest.previous.json").unlink()
    with pytest.raises(SystemExit, match="nothing to roll back to"):
        ops.rollback(s.site_dir)


def test_publish_needs_a_built_page(two_builds, tmp_path):
    with pytest.raises(SystemExit, match="build the page first"):
        ops.publish(two_builds[0].site_dir, tmp_path / "nope", tmp_path / "out")
