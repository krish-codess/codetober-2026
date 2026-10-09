"""The downloader against a real HTTP server on localhost that can be told to misbehave."""

from __future__ import annotations

import dataclasses
import hashlib
import random
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dots import fetch
from dots.config import read_json

from .conftest import make_settings

MEMBER = (b"hello " + b" ".join([b"0.25"] * 50) + b"\n") * 4000


class Flaky(BaseHTTPRequestHandler):
    """Serves one zip with range support. `fail` requests are answered 503 first; `status` overrides everything."""

    body = b""
    fail = 0
    status = 0
    ranges: list[str] = []

    def do_HEAD(self):
        self.reply(head=True)

    def do_GET(self):
        self.reply(head=False)

    def reply(self, head: bool):
        cls = type(self)
        if cls.status:
            return self.send_error(cls.status)
        if not head and cls.fail > 0:
            cls.fail -= 1
            return self.send_error(503)
        rng = self.headers.get("Range")
        lo, hi = 0, len(cls.body) - 1
        if rng:
            cls.ranges.append(rng)
            lo, hi = (int(v) for v in rng.removeprefix("bytes=").split("-"))
        self.send_response(206 if rng else 200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(hi - lo + 1))
        self.end_headers()
        if not head:
            self.wfile.write(cls.body[lo : hi + 1])

    def log_message(self, *a):
        pass


@pytest.fixture()
def server(tmp_path, monkeypatch):
    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        # A 12 MB incompressible member we must not download: larger than the reader's 8 MB buffer.
        zf.writestr("glove.twitter.27B.25d.txt", random.Random(0).randbytes(12 << 20))
        zf.writestr(fetch.GLOVE_MEMBER, MEMBER)
    Flaky.body, Flaky.fail, Flaky.status, Flaky.ranges = path.read_bytes(), 0, 0, []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)  # keep the backoff, skip the waiting
    yield f"http://127.0.0.1:{httpd.server_address[1]}/archive.zip"
    httpd.shutdown()


def settings_for(tmp_path, *urls, source=fetch.GLOVE):
    return dataclasses.replace(
        make_settings(tmp_path / "data"), source=source, glove_urls=urls, synth_rows=300
    )


def test_only_the_wanted_member_is_downloaded_and_it_is_verified(server, tmp_path):
    s = settings_for(tmp_path, server)
    assert fetch.run(s) == fetch.GLOVE
    raw = s.raw(fetch.GLOVE)
    assert (raw / fetch.GLOVE_MEMBER).read_bytes() == MEMBER
    meta = read_json(raw / "SOURCE.json")
    assert meta["files"][0]["sha256"] == hashlib.sha256(MEMBER).hexdigest() and meta["url"] == server
    fetched = sum(int(r.split("-")[1]) - int(r.split("=")[1].split("-")[0]) + 1 for r in Flaky.ranges)
    assert fetched < len(Flaky.body) / 2, "the decoy member was not transferred"

    Flaky.status = 500  # the source goes away: a second run must not need it
    assert fetch.run(s) == fetch.GLOVE
    assert fetch.current_source(dataclasses.replace(s, source="auto")) == fetch.GLOVE


def test_transient_failures_are_retried(server, tmp_path):
    Flaky.fail = 3
    s = settings_for(tmp_path, server)
    fetch.run(s)
    assert (s.raw(fetch.GLOVE) / fetch.GLOVE_MEMBER).read_bytes() == MEMBER


def test_a_client_error_is_not_retried_and_the_next_mirror_is_used(server, tmp_path):
    s = settings_for(tmp_path, server.replace("archive.zip", "missing.zip"), server)
    Flaky.status = 0
    original = Flaky.reply

    def reply(self, head):
        if "missing" in self.path:
            Flaky.ranges.append("404")
            return self.send_error(404)
        return original(self, head)

    Flaky.reply = reply
    try:
        fetch.run(s)
    finally:
        Flaky.reply = original
    assert Flaky.ranges.count("404") == 1, "a 404 will not get better by asking again"
    assert read_json(s.raw(fetch.GLOVE) / "SOURCE.json")["url"] == server


def test_a_dead_source_fails_loudly_or_falls_back_when_asked_to(server, tmp_path):
    Flaky.status = 503
    with pytest.raises(RuntimeError, match="could not fetch"):
        fetch.run(settings_for(tmp_path, server))
    assert not (settings_for(tmp_path, server).raw(fetch.GLOVE) / "SOURCE.json").exists(), (
        "no half-finished raw directory is left looking complete"
    )

    s = settings_for(tmp_path, server, source="auto")
    assert fetch.run(s) == "synth-300"
    assert fetch.current_source(s) == "synth-300"
    assert read_json(s.raw("synth-300") / "SOURCE.json")["url"] is None


def test_a_corrupt_archive_is_rejected(server, tmp_path):
    body = bytearray(Flaky.body)
    start = body.index(b"glove.twitter.27B.50d.txt") + 40
    body[start : start + 64] = bytes(64)  # damage inside the member's compressed stream
    Flaky.body = bytes(body)
    with pytest.raises(RuntimeError):
        fetch.run(settings_for(tmp_path, server))
    assert not (settings_for(tmp_path, server).raw(fetch.GLOVE) / fetch.GLOVE_MEMBER).exists()


def test_generator_is_deterministic(tmp_path):
    a = fetch.write_synth(tmp_path / "a.txt", 500, seed=3)
    b = fetch.write_synth(tmp_path / "b.txt", 500, seed=3)
    assert a == b and (tmp_path / "a.txt").read_bytes() == (tmp_path / "b.txt").read_bytes()
    fetch.write_synth(tmp_path / "c.txt", 500, seed=4)
    assert (tmp_path / "c.txt").read_bytes() != (tmp_path / "a.txt").read_bytes()
