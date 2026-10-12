"""The archive, the pump and the provider reader, with real files and real sockets (no Kafka needed)."""

import asyncio
import dataclasses
import json

import pytest
from conftest import FakeProducer

from ghostfleet import obs
from ghostfleet.collectors import Archive, Decoder, pump, read_provider
from ghostfleet.config import Settings
from ghostfleet.nmea import encode_position, encode_static, to_sentences
from ghostfleet.simulate import World

S = dataclasses.replace(Settings(), reconnect_base_s=0.01, reconnect_cap_s=0.05, idle_timeout_s=0.3)
T = 1_791_500_000.0


def sim_lines(seconds: int = 1200, ships: int = 150) -> list[str]:
    return [line for _, line in World(seed=5, n_ships=ships).sentences(int(T), int(T) + seconds)]


def keys(sent) -> set:
    """What a batch of records means, ignoring how many times each was delivered."""
    return {
        (topic, value.get("cid"), value.get("mmsi"), value.get("ts"), value.get("reason")) for topic, _, value in sent
    }


def test_archive_preserves_input_rolls_over_by_hour_and_hides_partial_lines(tmp_path):
    a = Archive(tmp_path, "relay")
    a.append(T, "first")
    a.append(T + 1, "second\twith a tab")
    a.append(T + 3600, "next hour")
    assert len(a.files()) == 2
    rows = a.read(None, 10)
    assert [(r[1], r[2]) for r in rows] == [(T, "first"), (T + 1, "second\twith a tab")]
    rows += a.read(rows[-1][3], 10)
    assert rows[-1][2] == "next hour" and rows[-1][0][0] == a.files()[1]
    assert a.read(rows[-1][3], 10) == []
    with (tmp_path / "relay" / a.files()[1]).open("ab") as f:  # a writer caught mid-line
        f.write(b"1791503601.000\thalf a li")
    assert a.read(rows[-1][3], 10) == []
    assert a.backlog_bytes(rows[-1][3]) == len(b"1791503601.000\thalf a li")
    a.save_checkpoint(rows[0][3])
    assert Archive(tmp_path, "relay").load_checkpoint() == rows[0][3]
    a.close()


def test_plain_capture_without_receive_times_is_readable(tmp_path):
    (tmp_path / "cap").mkdir()
    (tmp_path / "cap" / "x.nmea").write_bytes(b"!AIVDM,1,1,,A,13HOI:0P0000VOHLCnHQKwvL05Ip,0*23\r\n")
    (row,) = Archive(tmp_path, "cap").read(None, 10)
    assert (row[1], row[2]) == (None, "!AIVDM,1,1,,A,13HOI:0P0000VOHLCnHQKwvL05Ip,0*23")


async def run_pump(tmp_path, source: str, prod, **kw):
    archive = Archive(tmp_path, source)
    try:
        return await pump(archive, Decoder(source, S), prod, follow=False, **kw)
    finally:
        archive.close()


async def test_every_defect_reaches_the_dead_letter_topic_with_its_payload(tmp_path):
    a = Archive(tmp_path, "sim")
    lines = sim_lines()
    for i, line in enumerate(lines):
        a.append(T + i / 100, line)
    a.close()
    prod = FakeProducer()
    stats = await run_pump(tmp_path, "sim", prod)
    assert stats.lines == len(lines)
    by_topic: dict[str, list[dict]] = {}
    for topic, key, value in prod.sent:
        by_topic.setdefault(topic, []).append(value)
        assert key == (value["cid"] if topic == S.topic_dlq else value["mmsi"])  # partitioned by vessel
    reasons = {d["reason"] for d in by_topic[S.topic_dlq]}
    assert reasons >= {"bad_checksum", "malformed_sentence", "non_vessel_mmsi", "position_unavailable"}
    assert all(
        d["payload"] and d["cid"].startswith("sim/") and d["stage"] in ("decode", "validate")
        for d in by_topic[S.topic_dlq]
    )
    # Nothing vanished: every input line is a record, a dead letter, a continuation fragment or a well-formed type we skip.
    assert len(by_topic[S.topic_positions]) > 0.8 * len(lines)
    assert all(p["source"] == "sim" and p["v"] == 1 and "second" not in p for p in by_topic[S.topic_positions])


async def test_pump_resumes_from_checkpoint_after_kafka_dies_mid_batch(tmp_path):
    lines = sim_lines()
    for source in ("clean", "crashy"):
        a = Archive(tmp_path, source)
        for i, line in enumerate(lines):
            a.append(T + i / 100, line)
        a.close()
    clean = FakeProducer()
    await run_pump(tmp_path, "clean", clean)

    dying = FakeProducer(fail_after=len(clean.sent) // 3)
    with pytest.raises(ConnectionError):
        await run_pump(tmp_path, "crashy", dying)
    checkpoint = Archive(tmp_path, "crashy").load_checkpoint()
    assert checkpoint is not None and 0 < checkpoint[1] < (tmp_path / "crashy" / checkpoint[0]).stat().st_size
    survivor = FakeProducer()
    await run_pump(tmp_path, "crashy", survivor)  # a fresh process: new decoder, state only from disk

    def normalise(sent):
        return {(t, c.replace("crashy/", "clean/") if c else c, m, ts, r) for t, c, m, ts, r in keys(sent)}

    delivered = normalise(dying.sent) | normalise(survivor.sent)
    assert delivered == normalise(clean.sent)  # nothing lost...
    assert len(survivor.sent) < len(clean.sent)  # ...and the restart did not start over from the beginning


async def test_checkpoint_waits_for_a_fragment_still_missing_its_sibling(tmp_path):
    part1, part2 = to_sentences(*encode_static(538001234, 9134270, "V7AB1", "NORD TRADER", 70, 200, 32), seq=1)
    position = to_sentences(*encode_position(257000001, 60.0, 5.0, 10.0, 90.0, 90, 0))[0]
    a = Archive(tmp_path, "relay")
    a.append(T, position)
    a.append(T + 0.1, part1)
    a.append(T + 0.2, position)
    first = FakeProducer()
    await pump(a, Decoder("relay", S), first, follow=False)
    # The process dies here. The checkpoint must still point at part1, not past the line after it.
    assert a.load_checkpoint() == a.read(None, 1)[0][3]
    a.append(T + 0.3, part2)
    second = FakeProducer()
    await pump(a, Decoder("relay", S), second, follow=False)
    a.close()
    statics = [v for t, _, v in second.sent if t == S.topic_static]
    assert [s["name"] for s in statics] == ["NORD TRADER"]


async def test_stale_fragment_is_quarantined_and_releases_the_checkpoint(tmp_path):
    part1, _ = to_sentences(*encode_static(538001234, 9134270, "V7AB1", "NORD TRADER", 70, 200, 32), seq=1)
    position = to_sentences(*encode_position(257000001, 60.0, 5.0, 10.0, 90.0, 90, 0))[0]
    a = Archive(tmp_path, "relay")
    a.append(T, part1)
    a.append(T + S.fragment_timeout_s + 1, position)
    prod = FakeProducer()
    await pump(a, Decoder("relay", S), prod, follow=False)
    assert a.load_checkpoint() == a.end()
    a.close()
    (dead,) = [v for t, _, v in prod.sent if t == S.topic_dlq]
    assert (dead["reason"], dead["payload"], dead["stage"]) == ("incomplete_multipart", part1, "decode")


async def test_aisstream_documents_flow_through_the_same_pump(tmp_path):
    doc = {
        "MessageType": "PositionReport",
        "MetaData": {"time_utc": "2026-10-09 18:00:05.5 +0000 UTC"},
        "Message": {
            "PositionReport": {
                "UserID": 259000420,
                "Latitude": 66.0,
                "Longitude": 12.2,
                "Sog": 3.1,
                "Cog": 10.0,
                "TrueHeading": 11,
                "NavigationalStatus": 0,
                "Timestamp": 4,
            }
        },
    }
    a = Archive(tmp_path, "aisstream")
    a.append(T, json.dumps(doc))
    a.append(T, "{not json")
    a.append(T, json.dumps({"MessageType": "PositionReport", "Message": {}}))
    prod = FakeProducer()
    await pump(a, Decoder("aisstream", S), prod, kind="aisstream", follow=False)
    a.close()
    assert [(t, v.get("mmsi"), v.get("reason")) for t, _, v in prod.sent] == [
        (S.topic_positions, 259000420, None),
        (S.topic_dlq, None, "aisstream_JSONDecodeError"),
        (S.topic_dlq, None, "aisstream_KeyError"),
    ]


# --- provider connections: a real TCP server that misbehaves --------------------------------------------


async def test_reader_reconnects_with_backoff_and_archives_every_line(tmp_path):
    connections = 0

    async def flaky(reader, writer):
        nonlocal connections
        connections += 1
        n = connections
        if n == 2:
            await asyncio.sleep(1.0)  # a connection that goes silent: only the idle timeout can save us
        else:
            for i in range(50):
                writer.write(f"line {n}-{i}\r\n".encode())
            await writer.drain()
        writer.close()  # then the relay hangs up, every time

    server = await asyncio.start_server(flaky, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    archive = Archive(tmp_path, "flaky")
    task = asyncio.create_task(read_provider("flaky", f"tcp://127.0.0.1:{port}", archive, S))
    try:
        async with asyncio.timeout(10):
            while connections < 5:
                await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
    lines = [row[2] for row in archive.read(None, 10_000)]
    archive.close()
    for n in (1, 3, 4):
        assert [ln for ln in lines if ln.startswith(f"line {n}-")] == [f"line {n}-{i}" for i in range(50)]
    assert obs.CONNECTS.labels("flaky", "failed")._value.get() >= 4


async def test_reader_keeps_trying_while_the_provider_is_unreachable(tmp_path):
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()  # nothing listens here now
    archive = Archive(tmp_path, "down")
    task = asyncio.create_task(read_provider("down", f"tcp://127.0.0.1:{port}", archive, S))
    failed = obs.CONNECTS.labels("down", "failed")._value
    async with asyncio.timeout(20):  # Windows takes about two seconds to refuse a local connection
        while failed.get() < 3:
            await asyncio.sleep(0.05)
    assert not task.done()  # still alive, still retrying
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    archive.close()
