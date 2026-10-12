"""Shared fixtures. Integration tests run against a real PostGIS and a real Kafka, named by
GHOST_TEST_DSN and GHOST_TEST_KAFKA. Without them they skip; with GHOST_REQUIRE_INTEGRATION=1 (CI)
a missing dependency is a failure, so a misconfigured pipeline cannot go green by skipping."""

import asyncio
import dataclasses
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

from ghostfleet.collectors import Decoder
from ghostfleet.config import Settings
from ghostfleet.db import migrate
from ghostfleet.simulate import World
from ghostfleet.sink import write_batch

T_END = 1_791_568_800  # a fixed "now" for generated history: 2026-10-09 18:00 UTC
TABLES = "positions, vessel_latest, dark_events, coverage_cells, quarantine, vessels"


class FakeProducer:
    """Stands in for Kafka where Kafka is not what is under test. Can die after N sends."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list[tuple[str, object, dict]] = []
        self.fail_after = fail_after

    async def send(self, topic, value=None, key=None):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionError("broker gone")
        self.sent.append((topic, key, value))
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    async def send_and_wait(self, topic, value=None, key=None):
        await (await self.send(topic, value, key))


def _need(var: str) -> str:
    value = os.environ.get(var)
    if not value:
        if os.environ.get("GHOST_REQUIRE_INTEGRATION"):
            pytest.fail(f"{var} is not set but GHOST_REQUIRE_INTEGRATION is")
        pytest.skip(f"{var} is not set")
    return value


@pytest.fixture(scope="session")
def cfg() -> Settings:
    run = uuid.uuid4().hex[:8]
    return dataclasses.replace(
        Settings(), dsn=_need("GHOST_TEST_DSN"), kafka=os.environ.get("GHOST_TEST_KAFKA", ""), partitions=3,
        topic_positions=f"t{run}.positions", topic_static=f"t{run}.static", topic_dlq=f"t{run}.dlq", group=f"g{run}",
        reconnect_base_s=0.05, reconnect_cap_s=0.5,
    )  # fmt: skip


@pytest.fixture(scope="session")
def kafka_cfg(cfg: Settings) -> Settings:
    _need("GHOST_TEST_KAFKA")
    return cfg


@pytest_asyncio.fixture(scope="session")
async def schema(cfg: Settings) -> None:
    await migrate(cfg.dsn, 0)
    await migrate(cfg.dsn)


@pytest_asyncio.fixture
async def conn(cfg: Settings, schema: None):
    c = await asyncpg.connect(cfg.dsn)
    await c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    yield c
    await c.close()


@pytest.fixture(scope="session")
def history():
    """Six simulated hours of 1000 ships, decoded: (positions, statics, dead letters, ground truth)."""
    world = World(seed=7, n_ships=1000)
    decoder = Decoder("sim", Settings())
    out: dict[str, list[dict]] = {"ais.positions": [], "ais.static": [], "ais.dlq": []}
    for i, (_, line) in enumerate(world.sentences(T_END - 6 * 3600, T_END)):
        for topic, _, value in decoder.nmea(line, None, f"sim/history:{i}", ("history", i)):
            out[topic].append(value)
    return out["ais.positions"], out["ais.static"], out["ais.dlq"], world.truth


async def load(c: asyncpg.Connection, positions: list[dict], statics: list[dict] = (), dead: list[dict] = ()) -> int:
    inserted = 0
    for i in range(0, len(positions), 5000):
        inserted += len(await write_batch(c, positions[i : i + 5000], [], []))
    await write_batch(c, [], list(statics), list(dead))
    return inserted
