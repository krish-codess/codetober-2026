"""A seeded AIS world that speaks real NMEA, with the defects measured in docs/data-profile.md.

The simulator stands in for providers that are rate-limited, keyed or unreachable. It is consumed
exactly like a real relay (a TCP stream of AIVDM sentences, or an archive file of them) and it
knows the ground truth: for every silence it can say whether the transponder was off, a receiver
was down, or the vessel was simply out of range. That is what lets detection be scored, not asserted.

Everything is a pure function of (seed, time), so a live server and a generated history agree, and
a failing test reproduces exactly.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .geo import KNOT_MS, bearing_deg, destination, distance_m
from .nmea import checksum, encode_position, encode_static, to_sentences

log = logging.getLogger(__name__)

# (name, lat, lon, bearing towards open water). Three receivers each give a coverage blob ~250 km wide.
HUBS = [
    ("Rotterdam", 52.0, 3.9, 300), ("Bergen", 60.4, 5.0, 280), ("Tromso", 69.8, 18.5, 330), ("Gibraltar", 36.0, -5.6, 250),
    ("Piraeus", 37.8, 23.6, 170), ("Suez", 29.8, 32.6, 165), ("Dubai", 25.4, 55.2, 330), ("Mumbai", 18.9, 72.6, 250),
    ("Colombo", 6.9, 79.7, 240), ("Singapore", 1.2, 103.9, 110), ("Hong Kong", 22.1, 114.3, 150), ("Shanghai", 31.0, 122.4, 100),
    ("Busan", 35.0, 129.2, 140), ("Tokyo", 34.9, 139.9, 150), ("Sydney", -33.9, 151.4, 100), ("Durban", -29.9, 31.2, 120),
    ("Lagos", 6.3, 3.3, 190), ("Santos", -24.1, -46.2, 130), ("Panama", 8.8, -79.5, 190), ("Houston", 29.2, -94.6, 150),
    ("New York", 40.4, -73.7, 120), ("Los Angeles", 33.6, -118.4, 230), ("Vancouver", 48.4, -124.9, 260), ("Callao", -12.1, -77.3, 250),
]  # fmt: skip
RECEIVER_RANGE_M = 85_000
STEP_MOORED_S = 180  # AIS reports every 3 minutes at anchor
PERIOD_S = 6 * 3600  # dark vessels and receiver outages repeat on this cycle


@dataclass(slots=True)
class Receiver:
    lat: float
    lon: float
    outage_phase: int | None  # seconds into each PERIOD_S when a 50-minute outage starts, or None


@dataclass(slots=True)
class Ship:
    mmsi: int
    name: str
    callsign: str
    imo: int
    ship_type: int
    length: int
    class_b: bool
    heading_sensor: bool
    speed_kn: float  # 0 for moored
    routes: list[tuple[float, float, float, float, float]]  # lat_a, lon_a, lat_b, lon_b, length_m
    phase: int
    dark_s: int = 0  # seconds of transponder-off at the start of every PERIOD_S (0: never)
    hub: int = 0


@dataclass
class Truth:
    """What really caused one silence of a vessel."""

    mmsi: int
    start: int
    end: int
    cause: str  # dark | outage | range
    moving: bool
    teleport: bool

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _imo(rng: random.Random, valid: bool) -> int:
    six = [rng.randint(1, 9), *(rng.randint(0, 9) for _ in range(5))]
    check = sum(d * w for d, w in zip(six, range(7, 1, -1), strict=True)) % 10
    return int("".join(map(str, six))) * 10 + (check if valid else (check + 3) % 10)


@dataclass
class World:
    seed: int = 7
    n_ships: int = 500
    step_s: int = 60  # reporting interval of a moving vessel
    gap_s: int = 1800  # silences at least this long are recorded as ground truth
    defects: bool = True
    ships: list[Ship] = field(default_factory=list)
    receivers: list[list[Receiver]] = field(default_factory=list)  # per hub
    truth: list[Truth] = field(default_factory=list)
    _slots: dict[tuple[int, int], list[Ship]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        rng = random.Random(self.seed)
        for i, (_, lat, lon, sea) in enumerate(HUBS):
            sites = [(lat, lon), destination(lat, lon, sea + 90, 70_000), destination(lat, lon, sea - 90, 70_000)]
            # One receiver in every third hub has a recurring outage.
            self.receivers.append(
                [
                    Receiver(la, lo, rng.randrange(PERIOD_S) if i % 3 == 0 and j == 0 else None)
                    for j, (la, lo) in enumerate(sites)
                ]
            )
        for n in range(self.n_ships):
            hub = rng.randrange(len(HUBS))
            moored = rng.random() < 0.45
            kind = rng.random()
            dark_s = 0
            routes = [self._route(rng, hub, moored)]
            if not moored and kind < 0.06:  # goes dark inside coverage
                dark_s = rng.choice([2700, 3600, 5400, 7200])
                routes = [self._route(rng, hub, False, max_m=60_000)]
            elif not moored and kind < 0.08:  # goes dark and reappears at another hub: a teleport
                dark_s = rng.choice([2700, 3600])
                other = (hub + rng.randrange(1, len(HUBS))) % len(HUBS)
                routes = [self._route(rng, hub, False, max_m=60_000), self._route(rng, other, False, max_m=60_000)]
            mid = rng.choice([257, 258, 244, 235, 211, 538, 636, 477, 563, 351, 370, 412, 431, 440, 219, 265])
            self.ships.append(Ship(
                mmsi=mid * 1_000_000 + n, name=f"{rng.choice(['NORD', 'SEA', 'OCEAN', 'STAR', 'POLAR', 'GULF'])} {rng.choice(['TRADER', 'SPIRIT', 'EAGLE', 'PIONEER', 'QUEEN'])} {n}",
                callsign=f"L{rng.choice('ABCDEFGH')}{n % 10000:04d}", imo=_imo(rng, rng.random() > 0.15),
                ship_type=rng.choice([30, 52, 60, 70, 70, 70, 71, 80, 80, 89]), length=rng.randint(15, 330),
                class_b=rng.random() < 0.1, heading_sensor=rng.random() > 0.45, speed_kn=0.0 if moored else rng.uniform(7, 21),
                routes=routes, phase=rng.randrange(PERIOD_S), dark_s=dark_s, hub=hub,
            ))  # fmt: skip

    @staticmethod
    def _route(
        rng: random.Random, hub: int, moored: bool, max_m: float = 420_000
    ) -> tuple[float, float, float, float, float]:
        _, hlat, hlon, sea = HUBS[hub]
        a = destination(hlat, hlon, sea + rng.uniform(-70, 70), rng.uniform(2_000, 35_000))
        if moored:
            return (*a, *a, 0.0)
        # Most routes leave coverage (~85 km) and come back: honest, innocent coverage gaps.
        b = destination(hlat, hlon, sea + rng.uniform(-50, 50), rng.uniform(40_000, max_m))
        return (*a, *b, distance_m(*a, *b))

    # --- state at an instant -------------------------------------------------------------------

    def state(self, ship: Ship, t: int) -> tuple[float, float, float, float]:
        """lat, lon, speed over ground (knots), course over ground (degrees)."""
        la, lo, lb, lob, length = ship.routes[((t + ship.phase) // PERIOD_S) % len(ship.routes)]
        if length == 0:
            return la, lo, 0.0, 0.0
        travelled = (ship.speed_kn * KNOT_MS * (t + ship.phase)) % (2 * length)
        outbound = travelled <= length
        u = travelled / length if outbound else 2 - travelled / length
        course = bearing_deg(la, lo, lb, lob) if outbound else bearing_deg(lb, lob, la, lo)
        return la + (lb - la) * u, lo + (lob - lo) * u, ship.speed_kn, course

    def transponder_off(self, ship: Ship, t: int) -> bool:
        return (t + ship.phase) % PERIOD_S < ship.dark_s

    def reception(self, ship: Ship, lat: float, lon: float, t: int) -> str:
        """'heard', 'outage' (in range only of a receiver that is down) or 'range'."""
        in_range_of_down = False
        for hub in range(len(HUBS)):
            if abs(HUBS[hub][1] - lat) > 3:
                continue
            for r in self.receivers[hub]:
                if distance_m(lat, lon, r.lat, r.lon) <= RECEIVER_RANGE_M:
                    if r.outage_phase is not None and (t - r.outage_phase) % PERIOD_S < 3000:
                        in_range_of_down = True
                    else:
                        return "heard"
        return "outage" if in_range_of_down else "range"

    # --- emission -------------------------------------------------------------------------------

    def _step(self, ship: Ship) -> int:
        return self.step_s if ship.speed_kn else max(self.step_s, STEP_MOORED_S)

    def _due(self, t: int) -> Iterator[Ship]:
        """Ships whose reporting slot is second t. Slots are staggered by MMSI, as SOTDMA staggers them."""
        if not self._slots:
            for ship in self.ships:
                step = self._step(ship)
                self._slots.setdefault((step, -ship.mmsi % step), []).append(ship)
        for step in {k[0] for k in self._slots}:
            yield from self._slots.get((step, t % step), ())

    def reports(self, t: int, last: dict[int, list[Any]]) -> Iterator[tuple[Ship, float, float, float, float]]:
        """Reports actually received at second t, updating ground truth for vessels that were silent."""
        for ship in self._due(t):
            lat, lon, sog, cog = self.state(ship, t)
            rx = self.reception(ship, lat, lon, t)
            step = self._step(ship)
            seen = last.get(ship.mmsi)
            if rx == "heard" and self.transponder_off(ship, t):
                rx = "dark"
            if rx != "heard":
                if seen:
                    seen[1][rx] = seen[1].get(rx, 0) + step
                continue
            if seen and t - seen[0] >= self.gap_s:
                why = seen[1]
                cause = (
                    "dark"
                    if why.get("dark", 0) >= self.gap_s
                    else max(("outage", "range"), key=lambda c: why.get(c, 0))
                )
                moved = distance_m(seen[2], seen[3], lat, lon)
                self.truth.append(
                    Truth(ship.mmsi, seen[0], t, cause, ship.speed_kn > 0, moved / (t - seen[0]) / KNOT_MS > 60)
                )
            last[ship.mmsi] = [t, {}, lat, lon]
            yield ship, lat, lon, sog, cog

    def sentences(self, start: int, end: int) -> Iterator[tuple[int, str]]:
        """(delivery second, NMEA line) for [start, end), in delivery order, defects included."""
        rng = random.Random(f"{self.seed}:{start}")
        last: dict[int, list[Any]] = {}
        late: list[tuple[int, int, str]] = []
        seq = n = 0
        for t in range(start, end):
            out: list[str] = []
            for ship, lat, lon, sog, cog in self.reports(t, last):
                d = rng.random() if self.defects else 1.0
                if d < 0.012:  # GPS has no fix: the standard's "not available" position
                    lat, lon = 91.0, 181.0
                heading = round(cog) % 360 if ship.heading_sensor else None
                jitter = rng.uniform(-0.05, 0.05) if sog == 0 else 0.0
                payload, fill = encode_position(ship.mmsi, lat, lon, max(0.0, sog + jitter), cog if sog else None, heading, t % 60,
                                                class_b=ship.class_b, nav_status=0 if sog else 5)  # fmt: skip
                # A delayed line keeps its fix second but is stamped by the receiver when it finally arrives.
                delay = rng.randint(1, 30) if 0.012 <= d < 0.032 else 0
                (line,) = to_sentences(payload, fill, channel=rng.choice("AB"), tag_time=t + delay)
                if 0.032 <= d < 0.037:  # corrupted in transit: checksum no longer matches
                    line = line[:-6] + ("0" if line[-6] != "0" else "1") + line[-5:]
                elif 0.037 <= d < 0.040:  # cut off
                    line = line[: rng.randint(20, len(line) - 5)]
                if delay:
                    n += 1
                    heapq.heappush(late, (t + delay, n, line))
                else:
                    out.append(line)
                    if 0.040 <= d < 0.079:  # relays repeat themselves
                        out.append(line)
                if rng.random() < self.step_s / 1800:  # identity now and then; real relays thin these out too
                    seq += 1
                    parts = to_sentences(*encode_static(ship.mmsi, ship.imo, ship.callsign, ship.name, ship.ship_type, ship.length, ship.length // 6 + 3),
                                         seq=seq, channel="A", tag_time=t)  # fmt: skip
                    s = rng.random() if self.defects else 1.0
                    out.extend(parts[:1] if s < 0.03 else parts[::-1] if s < 0.04 else parts)
            if (
                self.defects and out and rng.random() < 0.015 * len(out)
            ):  # base stations and aids to navigation also talk
                payload, fill = encode_position(
                    rng.choice([2573100, 992570000 + rng.randrange(999)]), HUBS[0][1], HUBS[0][2], 0, None, None, t % 60
                )
                out.extend(to_sentences(payload, fill, tag_time=t))
            while late and late[0][0] <= t:
                out.append(heapq.heappop(late)[2])
            for line in out:
                yield t, line


def identity_check(line: str) -> bool:
    """True if a generated line still passes its own checksum (used by tests to count defects)."""
    body, _, ck = line.rpartition("!")[2].partition("*")
    return len(ck) >= 2 and ck[:2].isalnum() and f"{checksum(body):02X}" == ck[:2].upper()


async def serve(world: World, host: str, port: int, *, drop_every_s: int = 0) -> None:
    """A live relay: every second, send what the world's receivers heard to every connected client.

    drop_every_s > 0 closes all client connections on that cadence, to exercise reconnects.
    """
    clients: set[asyncio.StreamWriter] = set()

    async def on_connect(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        clients.add(writer)
        log.info("client connected", extra={"clients": len(clients)})

    server = await asyncio.start_server(on_connect, host, port)
    # One endless generator, pulled up to "now": delayed lines stay queued inside it until due.
    feed = world.sentences(int(time.time()) + 1, 2**62)
    ahead = next(feed)
    async with server:
        while True:
            await asyncio.sleep(max(0.0, ahead[0] - time.time()))
            now, lines = ahead[0], []
            while ahead[0] <= now:
                lines.append(ahead[1])
                ahead = next(feed)
            data = "".join(f"{line}\r\n" for line in lines).encode()
            drop = drop_every_s and now % drop_every_s == 0
            for w in list(clients):
                try:
                    if drop or w.is_closing():
                        raise ConnectionError
                    w.write(data)
                    if w.transport.get_write_buffer_size() > 4_000_000:  # a client that cannot keep up is cut loose
                        raise ConnectionError
                except (ConnectionError, RuntimeError):
                    clients.discard(w)
                    w.close()
