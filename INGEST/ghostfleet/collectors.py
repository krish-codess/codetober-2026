"""Provider connections, the raw archive, and the pump that turns archived lines into Kafka records.

Each provider gets two independent tasks:

    reader   socket -> append-only archive file        (never waits on Kafka)
    pump     archive -> decode -> validate -> Kafka    (checkpoints what Kafka has acknowledged)

The archive is the write-ahead log. A Kafka outage only makes the pump fall behind; a crash or
restart resumes the pump from its checkpoint; replaying an old window is the same pump pointed at
an old file. All three produce the same records, and the sink's key makes repeats harmless.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import websockets
from aiokafka import AIOKafkaProducer

from . import obs
from .bus import dead_letter, producer
from .config import Settings
from .nmea import NmeaError, Reassembler, decode_payload, parse_sentence
from .validate import is_vessel_mmsi, position_time, validate_position

log = logging.getLogger(__name__)
Record = tuple[str, int | str, dict[str, Any]]  # topic, key, value
Position = tuple[str, int]  # archive file name, byte offset


class Backoff:
    """Exponential backoff with full jitter: uniform(0, min(cap, base * 2^attempt))."""

    def __init__(self, base: float, cap: float, rng: Callable[[], float] = random.random) -> None:
        self.base, self.cap, self.rng, self.attempt = base, cap, rng, 0

    def next(self) -> float:
        ceiling = min(self.cap, self.base * 2**self.attempt)
        self.attempt += 1
        return float(self.rng() * ceiling)

    def reset(self) -> None:
        self.attempt = 0


class Archive:
    """Immutable-once-written raw input, one file per source per UTC hour: `<received_at>\\t<line>`."""

    def __init__(self, root: Path, source: str, pattern: str = "*.nmea") -> None:
        self.dir, self.source, self.pattern = root / source, source, pattern
        self.dir.mkdir(parents=True, exist_ok=True)
        self.wrote = asyncio.Event()
        self._out: Any = None
        self._out_name = ""
        self._in: Any = None
        self._in_name = ""

    def append(self, received_at: float, line: str) -> None:
        name = datetime.fromtimestamp(received_at, UTC).strftime("%Y%m%dT%H.nmea")
        if name != self._out_name:
            if self._out:
                self._out.close()
            self._out, self._out_name = (self.dir / name).open("ab"), name
        self._out.write(f"{received_at:.3f}\t{line}\n".encode("ascii", "replace"))
        self._out.flush()  # to the OS on every line: a process crash loses nothing
        self.wrote.set()

    def sync(self) -> None:
        if self._out:
            os.fsync(self._out.fileno())  # to the disk about once a second: a power cut loses at most that

    def files(self) -> list[str]:
        return sorted(p.name for p in self.dir.glob(self.pattern))

    def end(self) -> Position | None:
        files = self.files()
        return (files[-1], (self.dir / files[-1]).stat().st_size) if files else None

    def read(self, pos: Position | None, max_lines: int) -> list[tuple[Position, float | None, str, Position]]:
        """Complete lines after `pos` as (where it starts, receive time, line, where the next starts)."""
        files = self.files()
        if not files:
            return []
        name, offset = pos or (files[0], 0)
        while True:
            if name != self._in_name:
                if self._in:
                    self._in.close()
                self._in, self._in_name = (self.dir / name).open("rb"), name
            self._in.seek(offset)
            out: list[tuple[Position, float | None, str, Position]] = []
            while len(out) < max_lines:
                raw = self._in.readline()
                if not raw.endswith(b"\n"):  # nothing more, or the writer is mid-line
                    break
                stamp, tab, text = raw.rstrip(b"\r\n").decode("ascii", "replace").partition("\t")
                nxt = (name, offset + len(raw))
                # Plain captures (no receive-time column) are replayable too.
                out.append(((name, offset), float(stamp), text, nxt) if tab else ((name, offset), None, stamp, nxt))
                offset = nxt[1]
            later = [f for f in files if f > name]
            if out or not later:
                return out
            name, offset = later[0], 0

    @property
    def _checkpoint(self) -> Path:
        return self.dir / "checkpoint.json"

    def load_checkpoint(self) -> Position | None:
        try:
            d = json.loads(self._checkpoint.read_text())
            return d["file"], d["offset"]
        except (OSError, ValueError, KeyError):
            return None

    def save_checkpoint(self, pos: Position) -> None:
        tmp = self._checkpoint.with_suffix(".tmp")
        tmp.write_text(json.dumps({"file": pos[0], "offset": pos[1]}))
        os.replace(tmp, self._checkpoint)

    def backlog_bytes(self, pos: Position | None) -> int:
        files = self.files()
        start = pos or ((files[0], 0) if files else None)
        if start is None:
            return 0
        return sum((self.dir / f).stat().st_size for f in files if f >= start[0]) - start[1]

    def close(self) -> None:
        for f in (self._out, self._in):
            if f:
                f.close()
        self._out = self._in = None
        self._out_name = self._in_name = ""


# --- decoding one archived line into Kafka records --------------------------------------------


class Decoder:
    def __init__(self, source: str, s: Settings) -> None:
        self.source, self.s = source, s
        self.reasm = Reassembler(timeout_s=s.fragment_timeout_s)

    def _dlq(self, stage: str, reason: str, payload: str, received_at: float, cid: str) -> Record:
        obs.QUARANTINED.labels(stage, reason).inc()
        return self.s.topic_dlq, cid, dead_letter(stage, reason, self.source, payload, received_at, cid)

    def emit(self, msg: dict[str, Any], received_at: float, cid: str) -> Iterator[Record]:
        """Validate one decoded message and wrap it for its topic."""
        envelope = {"v": 1, "source": self.source, "received_at": received_at, "cid": cid}
        static = msg.pop("static", None)
        if msg["kind"] == "position":
            reason = validate_position(msg)
            if reason:
                yield self._dlq("validate", reason, json.dumps(msg), received_at, cid)
                return
            yield self.s.topic_positions, msg["mmsi"], {**msg, **envelope}
        elif not is_vessel_mmsi(msg.get("mmsi")):
            yield self._dlq("validate", "non_vessel_mmsi", json.dumps(msg), received_at, cid)
        else:
            yield self.s.topic_static, msg["mmsi"], {**msg, **envelope}
        if static:
            yield from self.emit(static, received_at, cid)

    def nmea(self, line: str, received_at: float | None, cid: str, pos: Position) -> Iterator[Record]:
        fallback = received_at if received_at is not None else time.time()
        try:
            sentence = parse_sentence(line)
        except NmeaError as e:
            yield self._dlq("decode", e.reason, line, fallback, cid)
            return
        # A receiver timestamp in the tag block beats ours: it is closer to the transmission.
        rx = sentence.tag_time if sentence.tag_time is not None else fallback
        self.reasm.expire(rx)
        done = self.reasm.feed(sentence, line, rx, ref=(cid, pos))
        for d in self.reasm.take_dropped():
            yield self._dlq("decode", d.reason, "\n".join(d.lines), rx, d.ref[0])
        if done is None:
            return
        try:
            msg = decode_payload(*done)
        except NmeaError as e:
            yield self._dlq("decode", e.reason, line, rx, cid)
            return
        if msg is None:
            obs.IGNORED.labels(self.source).inc()
            return
        if msg["kind"] == "position":
            msg["ts"] = position_time(rx, msg.pop("second"))
        yield from self.emit(msg, rx, cid)

    def aisstream(self, line: str, received_at: float | None, cid: str, pos: Position) -> Iterator[Record]:
        rx = received_at if received_at is not None else time.time()
        try:
            msg = aisstream_message(json.loads(line))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            yield self._dlq("decode", f"aisstream_{type(e).__name__}", line, rx, cid)
            return
        if msg is None:
            obs.IGNORED.labels(self.source).inc()
            return
        yield from self.emit(msg, rx, cid)

    def safe_position(self) -> Position | None:
        ref = self.reasm.oldest_ref()
        return ref[1] if ref else None


_AISSTREAM_POSITION = {"PositionReport": 1, "StandardClassBPositionReport": 18}


def _none_at(value: Any, limit: float) -> Any:
    return None if value is None or value >= limit else value


def aisstream_message(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Map one AISStream JSON document onto the same message shape the NMEA decoder produces."""
    kind = doc["MessageType"]
    body = doc["Message"][kind]
    if kind in _AISSTREAM_POSITION:
        stamp = doc["MetaData"]["time_utc"][:19]  # "2022-12-29 18:22:32.318353 +0000 UTC"
        rx = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
        heading = _none_at(body.get("TrueHeading"), 360)
        return {
            "kind": "position", "msg_type": _AISSTREAM_POSITION[kind], "mmsi": body["UserID"],
            "nav_status": body.get("NavigationalStatus"), "sog": _none_at(body.get("Sog"), 102.3),
            "lon": body["Longitude"], "lat": body["Latitude"], "cog": _none_at(body.get("Cog"), 360),
            "heading": heading, "ts": position_time(rx, body.get("Timestamp")),
        }  # fmt: skip
    if kind == "ShipStaticData":
        dim = body.get("Dimension") or {}
        return {
            "kind": "static", "msg_type": 5, "mmsi": body["UserID"], "imo": body.get("ImoNumber") or None,
            "callsign": (body.get("CallSign") or "").strip() or None, "name": (body.get("Name") or "").strip() or None,
            "ship_type": body.get("Type"), "to_bow": dim.get("A", 0), "to_stern": dim.get("B", 0),
            "to_port": dim.get("C", 0), "to_starboard": dim.get("D", 0),
            "draught": body.get("MaximumStaticDraught") or None,
            "destination": (body.get("Destination") or "").strip() or None,
        }  # fmt: skip
    return None


# --- the pump: archive -> Kafka -------------------------------------------------------------


@dataclass
class PumpStats:
    lines: int = 0
    records: int = 0


async def pump(
    archive: Archive, decoder: Decoder, prod: AIOKafkaProducer, *, kind: str = "nmea", follow: bool = True,
    checkpoint: bool = True, start: Position | None = None, batch_lines: int = 500,
) -> PumpStats:  # fmt: skip
    """Decode archived lines and produce them. With follow=False, stop at end of archive (a replay)."""
    decode = decoder.aisstream if kind == "aisstream" else decoder.nmea
    pos = archive.load_checkpoint() if checkpoint else start
    stats = PumpStats()
    while True:
        batch = archive.read(pos, batch_lines)
        if not batch:
            if not follow:
                return stats
            archive.wrote.clear()
            try:
                await asyncio.wait_for(archive.wrote.wait(), 1.0)
            except TimeoutError:
                pass
            continue
        obs.correlation_id.set(f"{archive.source}/{batch[0][0][0]}:{batch[0][0][1]}")
        pending = []
        kinds: dict[str, int] = {}
        for at, received_at, line, nxt in batch:
            cid = f"{archive.source}/{at[0]}:{at[1]}"
            for topic, key, value in decode(line, received_at, cid, at):
                pending.append(await prod.send(topic, value=value, key=key))
                kinds[topic] = kinds.get(topic, 0) + 1
            pos = nxt
        if pending:
            await asyncio.gather(*pending)  # every record acknowledged by the broker, or we raise
        stats.lines += len(batch)
        stats.records += len(pending)
        for topic, n in kinds.items():
            obs.PRODUCED.labels(archive.source, topic).inc(n)
        if checkpoint and pos:
            # Never checkpoint past a fragment still waiting for its siblings: after a restart it
            # must be read again, or the message it belongs to would be lost.
            safe = decoder.safe_position() or pos
            archive.save_checkpoint(safe)
            obs.ARCHIVE_BACKLOG.labels(archive.source).set(archive.backlog_bytes(pos))
        obs.LAST_SUCCESS.set_to_current_time()


# --- the reader: provider -> archive ----------------------------------------------------------


async def tcp_lines(url: str, s: Settings) -> AsyncIterator[str]:
    parts = urlsplit(url)
    reader, writer = await asyncio.wait_for(asyncio.open_connection(parts.hostname, parts.port), 10)
    try:
        while True:
            raw = await asyncio.wait_for(reader.readline(), s.idle_timeout_s)
            if not raw:
                raise ConnectionError("closed by peer")
            if line := raw.decode("ascii", "replace").strip():
                yield line
    finally:
        writer.close()


async def aisstream_lines(url: str, s: Settings) -> AsyncIterator[str]:
    if not s.aisstream_key:
        raise PermissionError("AISSTREAM_API_KEY is not set")
    async with websockets.connect(url, open_timeout=10) as ws:
        await ws.send(json.dumps({
            "APIKey": s.aisstream_key, "BoundingBoxes": [[[-90, -180], [90, 180]]],
            "FilterMessageTypes": [*_AISSTREAM_POSITION, "ShipStaticData"],
        }))  # fmt: skip
        while True:
            raw = await asyncio.wait_for(ws.recv(), s.idle_timeout_s)
            yield (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).replace("\n", " ")


def kind_of(url: str) -> str:
    return "aisstream" if url.startswith(("ws://", "wss://")) else "nmea"


async def read_provider(name: str, url: str, archive: Archive, s: Settings, *, stable_after_s: float = 10.0) -> None:
    """Hold a connection open forever. Every failure is logged, counted and retried with backoff."""
    lines = aisstream_lines if kind_of(url) == "aisstream" else tcp_lines
    backoff = Backoff(s.reconnect_base_s, s.reconnect_cap_s)
    last_sync = 0.0
    while True:
        connected_at = time.monotonic()
        up = False
        try:
            async for line in lines(url, s):
                if not up:
                    up = True
                    obs.CONNECTED.labels(name).set(1)
                    obs.CONNECTS.labels(name, "ok").inc()
                    log.info("provider connected", extra={"source": name})
                now = time.time()
                archive.append(now, line)
                obs.SENTENCES.labels(name).inc()
                if now - last_sync > 1:
                    archive.sync()
                    last_sync = now
            error: BaseException = ConnectionError("stream ended")
        except (OSError, TimeoutError, ValueError, websockets.WebSocketException, PermissionError) as e:
            error = e
        obs.CONNECTED.labels(name).set(0)
        obs.CONNECTS.labels(name, "failed").inc()
        # Only a connection that held for a while earns a fresh backoff; a flapping one keeps climbing.
        if time.monotonic() - connected_at > stable_after_s:
            backoff.reset()
        delay = backoff.next()
        log.warning(
            "provider down, reconnecting", extra={"source": name, "error": repr(error), "retry_in_s": round(delay, 2)}
        )
        await asyncio.sleep(delay)


async def start_producer(s: Settings) -> AIOKafkaProducer:
    backoff = Backoff(s.reconnect_base_s, s.reconnect_cap_s)
    while True:
        prod = producer(s)
        try:
            await prod.start()
            return prod
        except Exception as e:
            await prod.stop()
            delay = backoff.next()
            log.warning("kafka unavailable, retrying", extra={"error": repr(e), "retry_in_s": round(delay, 2)})
            await asyncio.sleep(delay)


async def pump_forever(archive: Archive, name: str, url: str, s: Settings) -> None:
    """Run the pump across Kafka outages. Each restart resumes from the checkpoint."""
    backoff = Backoff(s.reconnect_base_s, s.reconnect_cap_s)
    while True:
        prod = await start_producer(s)
        try:
            await pump(archive, Decoder(name, s), prod, kind=kind_of(url))
        except Exception as e:
            delay = backoff.next()
            log.warning(
                "pump failed, resuming from checkpoint",
                extra={"source": name, "error": repr(e), "retry_in_s": round(delay, 2)},
            )
            await asyncio.sleep(delay)
        finally:
            await prod.stop()


async def run(s: Settings) -> None:
    if not s.sources:
        raise SystemExit("GHOST_SOURCES is empty: nothing to collect")
    obs.serve_metrics(s.metrics_port)
    async with asyncio.TaskGroup() as tg:
        for name, url in s.sources.items():
            archive = Archive(s.archive_dir, name)
            tg.create_task(read_provider(name, url, archive, s))
            tg.create_task(pump_forever(archive, name, url, s))


async def replay(path: Path, source: str, s: Settings) -> PumpStats:
    """Push an archived file (or a directory of them) through the normal decode path, start to end."""
    archive = (
        Archive(path.parent.parent, path.parent.name, path.name) if path.is_file() else Archive(path.parent, path.name)
    )
    prod = await start_producer(s)
    try:
        return await pump(archive, Decoder(source, s), prod, kind="nmea", follow=False, checkpoint=False)
    finally:
        await prod.stop()
        archive.close()
