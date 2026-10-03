"""Synthetic economy: an ESI-shaped feed with the real feed's statistical shape and defects.

Why: real ESI gives one live order book per call and no listing-level history, so a year of
auction snapshots (needed to exercise snapshot ingestion, fill inference, manipulation
detection and patch shocks with known ground truth) can only come from simulation.

Contract with the rest of the system:
- Output is written to the raw store in exactly the formats the real ESI ingestion writes
  (order-book bundles, patch-notes RSS) so parse.py consumes both identically.
- Calibrated from the real data profile (price level, book depth, ask dispersion per item).
- Deterministic: the same (config, until) always yields byte-identical raw files. The
  simulation is re-run from `start` on every tick and only payloads that have "arrived" by
  `until` are written; already-written payloads are no-ops in the content-addressed store.
- Ground truth (fair prices, true fills, injected manipulations) is written to the lake under
  synthetic_truth/ for evaluation only. The pipeline never reads it.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from goldstandard.raw import RawStore

SOURCE = "synthetic"
WORLD = "synthetic"
SERVER_NAMES = ["aurora", "borealis", "cinder", "drift", "ember", "frost", "gale", "haven"]
# Book depth relative to Jita (The Forge). The smallest servers are deliberately thin markets.
SERVER_SIZE = [0.5, 0.2, 0.08, 0.035, 0.02, 0.3, 0.12, 0.05]
DIV_VOL = {
    "minerals": 0.012,
    "fuel": 0.012,
    "industrial": 0.018,
    "ships": 0.015,
    "equipment": 0.02,
    "consumables": 0.015,
    "services": 0.012,
}
DURATIONS = np.array([1, 3, 7, 14, 30, 90])
DURATION_P = np.array([0.05, 0.1, 0.15, 0.2, 0.3, 0.2])
SALES_TAX = 0.036
BROKER_FEE = 0.015


@dataclass(frozen=True)
class Effect:
    divisions: tuple[str, ...]  # empty = all
    log_change: float
    ramp_days: float


@dataclass(frozen=True)
class PatchSpec:
    day: int  # offset from start
    patch_id: str
    title: str
    notes: str
    effects: tuple[Effect, ...] = ()
    inflation_per_day: float | None = None  # new world drift from this patch on
    bounty_multiplier: float | None = None
    is_major: bool = False


PATCHES: tuple[PatchSpec, ...] = (
    PatchSpec(12, "s1.01", "Patch S1.01", "Defect fixes: overview columns, drone UI tooltips."),
    PatchSpec(
        45,
        "s1.10",
        "Version S1.10 - Deep Core",
        "Mining: ore yields increased by 40% for all mining "
        "barges. Reprocessing efficiency of veldspar and scordite improved. Mineral output expected to rise.",
        (Effect(("minerals",), -0.36, 4.0), Effect(("ships",), -0.05, 10.0)),
        is_major=True,
    ),
    PatchSpec(71, "s1.11", "Patch S1.11", "Audio fixes; corrected localisation strings in the agency."),
    PatchSpec(
        110,
        "s1.20",
        "Version S1.20 - Bounty Season",
        "Rewards: NPC bounty payouts increased by 80% in "
        "null-security space. Mission rewards adjusted upward. Expect more ISK entering the economy.",
        inflation_per_day=0.0011,
        bounty_multiplier=1.8,
        is_major=True,
    ),
    PatchSpec(132, "s1.21", "Patch S1.21", "Fixed a crash when opening the fitting window; UI polish."),
    PatchSpec(171, "s1.22", "Patch S1.22", "Server stability improvements; tooltip fixes."),
    PatchSpec(
        200,
        "s1.30",
        "Version S1.30 - Cold Fusion",
        "Industry: fuel block blueprints now require 50% more "
        "ice products. Structure fuel consumption rebalanced; isotope and fuel demand will rise.",
        (Effect(("fuel",), 0.42, 2.0), Effect(("industrial",), 0.08, 5.0)),
        is_major=True,
    ),
    PatchSpec(231, "s1.31", "Patch S1.31", "Graphics: nebula rendering fixes. No gameplay changes."),
    PatchSpec(
        290,
        "s1.40",
        "Version S1.40 - Open Market",
        "Economy: PLEX and skill injector supply from the "
        "new store flooded; market sales tax reduced to 2%. Ship hull production costs lowered.",
        (Effect(("services",), -0.62, 0.25), Effect(("ships",), -0.18, 1.5), Effect(("equipment",), -0.08, 3)),
        inflation_per_day=0.0002,
        bounty_multiplier=1.1,
        is_major=True,
    ),
    PatchSpec(311, "s1.41", "Patch S1.41", "Fixed an issue with fleet warp; minor UI tweaks."),
    PatchSpec(352, "s1.42", "Patch S1.42", "Drone AI improvements for sentry drones; module tooltips."),
)
# A real shock with no patch behind it (e.g. a player war): attribution must report it as unexplained.
UNPATCHED_SHOCKS: tuple[tuple[int, Effect], ...] = ((252, Effect(("equipment", "consumables"), 0.22, 1.0)),)
BASE_INFLATION_PER_DAY = 0.00025


@dataclass(frozen=True)
class SynthConfig:
    start: date
    servers: int = 4
    snapshots_per_day: int = 4
    seed: int = 20251001
    defect_scale: float = 1.0
    items: tuple[int, ...] | None = None  # None = every calibrated item

    @property
    def server_ids(self) -> list[str]:
        return [f"syn-{n}" for n in SERVER_NAMES[: self.servers]]


@dataclass
class Book:
    oid: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    price: np.ndarray = field(default_factory=lambda: np.zeros(0))
    remain: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    total: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    issued: np.ndarray = field(default_factory=lambda: np.zeros(0))  # epoch seconds
    duration: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    buy: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    tag: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int8))  # 0 normal, 1 absurd, 2 bait, 3 corner

    def keep(self, mask: np.ndarray) -> None:
        for k in ("oid", "price", "remain", "total", "issued", "duration", "buy", "tag"):
            setattr(self, k, getattr(self, k)[mask])

    def add(self, **cols: np.ndarray) -> None:
        for k, v in cols.items():
            setattr(self, k, np.concatenate([getattr(self, k), v]))


def load_calibration(reference_dir: Path) -> dict[int, dict[str, Any]]:
    data = json.loads((reference_dir / "synthetic_calibration.json").read_text())
    return {int(k): v for k, v in data["items"].items()}


def _epoch(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()


def world_drift(cfg: SynthConfig, n_days: int) -> np.ndarray:
    """Cumulative log inflation per day: base drift, changed by economy patches."""
    rate = np.full(n_days, BASE_INFLATION_PER_DAY)
    for p in PATCHES:
        if p.inflation_per_day is not None and p.day < n_days:
            rate[p.day :] = p.inflation_per_day
    return np.concatenate([[0.0], np.cumsum(rate)[:-1]])


def effect_path(division: str, t_days: np.ndarray) -> np.ndarray:
    out = np.zeros_like(t_days)
    effects = [(p.day + 11 / 24, e) for p in PATCHES for e in p.effects]
    effects += [(d + 0.5, e) for d, e in UNPATCHED_SHOCKS]
    for t0, e in effects:
        if e.divisions and division not in e.divisions:
            continue
        frac = np.clip((t_days - t0) / max(e.ramp_days, 1e-6), 0, 1)
        out += e.log_change * frac
    return out


def simulate_series(
    cfg: SynthConfig,
    server_idx: int,
    item_id: int,
    division: str,
    cal: dict[str, Any],
    n_steps: int,
    truth: dict[str, list[Any]],
) -> Iterator[Book]:
    """Simulate one (server, item) order book; yields the book after each snapshot step."""
    rng = np.random.default_rng([cfg.seed, server_idx, item_id])
    spd = cfg.snapshots_per_day
    size = SERVER_SIZE[server_idx]
    step_s = 86400 / spd
    t0 = _epoch(cfg.start)
    t_days = np.arange(n_steps) / spd
    n_days = math.ceil(n_steps / spd) + 1

    elasticity = {"services": 0.6, "minerals": 1.0}.get(division, 0.9)
    drift = world_drift(cfg, n_days)
    log_fair = (
        math.log(cal["p0"])
        + rng.normal(0, 0.04)  # persistent server price level offset
        + elasticity * np.interp(t_days, np.arange(n_days), drift)
        + effect_path(division, t_days)
    )
    sigma = DIV_VOL.get(division, 0.015) / math.sqrt(spd)
    noise = np.zeros(n_steps)
    # separate stream: its length depends on the horizon, so it must not shift the order-flow draws
    eps = np.random.default_rng([cfg.seed, server_idx, item_id, 1]).normal(0, sigma, n_steps)
    for k in range(1, n_steps):
        noise[k] = 0.995 * noise[k - 1] + eps[k]
    fair = np.exp(log_fair + noise)

    n_target = max(1.0, cal["n_sell"] * size)
    disp = cal["disp"]
    vol_step = cal["vol0"] * size / spd
    per_order = max(1.0, vol_step * 6 / n_target)
    oid_base = (server_idx + 1) * 10**12 + item_id * 10**6
    next_oid = 0
    book = Book()
    corner_until = -1
    thin = n_target < 6

    def new_orders(n: int, f: float, step: int, buy: bool) -> None:
        nonlocal next_oid
        if n <= 0:
            return
        mu, lo = (-0.05, 0.6) if buy else (0.03, 0.9)
        prices = f * np.maximum(lo, np.exp(mu + disp * rng.standard_normal(n)))
        tot = np.maximum(1, np.round(rng.lognormal(math.log(per_order), 0.9, n))).astype(np.int64)
        ids = oid_base + next_oid + np.arange(n)
        next_oid += n
        book.add(
            oid=ids,
            price=np.round(prices, 2).clip(0.01),
            remain=tot.copy(),
            total=tot,
            issued=t0 + step * step_s - rng.uniform(0, step_s, n),
            duration=rng.choice(DURATIONS, n, p=DURATION_P),
            buy=np.full(n, buy),
            tag=np.zeros(n, np.int8),
        )

    for k in range(n_steps):
        f = fair[k]
        now = t0 + k * step_s
        day = k // spd
        # expiry and cancellations
        alive = (now - book.issued) < book.duration * 86400
        alive &= (rng.random(book.oid.size) > 0.015) | (book.tag == 1)  # trolls never cancel
        book.keep(alive)
        # repricing: honest sellers whose ask drifted far from the market modify it (EVE players
        # undercut constantly; a modified order keeps its order_id and remaining volume)
        if book.oid.size:
            drift_ = np.log(book.price / f) - np.where(book.buy, -0.05, 0.03)
            stale = (book.tag == 0) & (np.abs(drift_) > 2.5 * disp) & (rng.random(book.oid.size) < 0.6)
            if stale.any():
                mu = np.where(book.buy[stale], -0.05, 0.03)
                book.price[stale] = np.round(f * np.exp(mu + disp * rng.standard_normal(int(stale.sum()))), 2).clip(
                    0.01
                )
        # demand: buyers lift the cheapest asks they are willing to pay for
        sells = np.flatnonzero(~book.buy)
        qty = round(vol_step * rng.lognormal(0, 0.35))
        if k < corner_until:
            qty = 0  # cornered market: nobody pays the cornerer's price, and new supply is bought out
        if sells.size and qty > 0:
            order = sells[np.argsort(book.price[sells], kind="stable")]
            ok = order[book.price[order] <= f * 1.35]
            cum = np.cumsum(book.remain[ok])
            take = np.minimum(book.remain[ok], np.maximum(0, qty - (cum - book.remain[ok])))
            book.remain[ok] -= take
            filled = take.sum()
            if filled:
                truth["fills"].append((server_idx, item_id, day, int(filled), float((take * book.price[ok]).sum())))
        book.keep(book.remain > 0)
        # supply
        n_sell = int((~book.buy).sum())
        lam = max(0.2, (n_target - n_sell) * 0.6) + n_target * 0.06
        if k >= corner_until:
            new_orders(int(rng.poisson(lam)), f, k, False)
        n_buy = int(book.buy.sum())
        new_orders(int(rng.poisson(max(0.1, (n_target * 0.4 - n_buy) * 0.5))), f, k, True)
        # manipulation injections (ground truth recorded)
        u = rng.random(3)
        if u[0] < 0.002 * cfg.defect_scale:  # troll listing: absurd ask that sits in the book
            factor = 10 ** rng.uniform(1.5, 6)
            book.add(
                oid=np.array([oid_base + next_oid]),
                price=np.array([round(f * factor, 2)]),
                remain=np.array([3]),
                total=np.array([3]),
                issued=np.array([now - 60.0]),
                duration=np.array([90]),
                buy=np.array([False]),
                tag=np.array([1], np.int8),
            )
            next_oid += 1
            truth["manip"].append((server_idx, item_id, day, "absurd_listing", factor))
        if u[1] < 0.002 * cfg.defect_scale:  # bait: one unit far below fair
            factor = rng.uniform(0.005, 0.3)
            book.add(
                oid=np.array([oid_base + next_oid]),
                price=np.array([max(0.01, round(f * factor, 2))]),
                remain=np.array([1]),
                total=np.array([1]),
                issued=np.array([now - 30.0]),
                duration=np.array([1]),
                buy=np.array([False]),
                tag=np.array([2], np.int8),
            )
            next_oid += 1
            truth["manip"].append((server_idx, item_id, day, "bait_listing", factor))
        if thin and k >= corner_until and u[2] < (1 / 240) * cfg.defect_scale:  # corner a thin market
            hold = int(rng.integers(2, 6)) * spd
            factor = rng.uniform(2.0, 4.0)
            asks = ~book.buy
            if asks.any():  # the cornerer buys out every ask: these are genuine fills
                truth["fills"].append(
                    (
                        server_idx,
                        item_id,
                        day,
                        int(book.remain[asks].sum()),
                        float((book.remain[asks] * book.price[asks]).sum()),
                    )
                )
            book.keep(book.buy)
            n = int(rng.integers(2, 4))
            book.add(
                oid=oid_base + next_oid + np.arange(n),
                price=np.round(f * factor * rng.uniform(1, 1.05, n), 2),
                remain=np.full(n, 5),
                total=np.full(n, 5),
                issued=np.full(n, now - 10.0),
                duration=np.full(n, 7),
                buy=np.zeros(n, bool),
                tag=np.full(n, 3, np.int8),
            )
            next_oid += n
            corner_until = k + hold
            truth["manip"].append((server_idx, item_id, day, "corner", factor))
        if k % spd == spd // 2:
            truth["fair"].append((server_idx, item_id, day, float(f)))
        yield book


# ------------------------------------------------------------------------------------------ rendering
def render_page(book: Book, item_id: int, region: int, rng: np.random.Generator, scale: float) -> str:
    """One ESI orders page for an item, with the defects seen (or plausible) in the real feed."""
    n = book.oid.size
    u = rng.random((n, 8)).tolist()
    rows: list[dict[str, Any]] = []
    iso = np.datetime_as_string(book.issued.astype("datetime64[s]"), unit="s").tolist()
    dur, buy, oid = book.duration.tolist(), book.buy.tolist(), book.oid.tolist()
    price, remain, total = book.price.tolist(), book.remain.tolist(), book.total.tolist()
    loc, system = 60000000 + region % 1000, 30000000 + region % 1000
    for i in range(n):
        row: dict[str, Any] = {
            "duration": dur[i],
            "is_buy_order": buy[i],
            "issued": iso[i] + "Z",
            "location_id": loc,
            "min_volume": 1,
            "order_id": oid[i],
            "price": price[i],
            "range": "region",
            "system_id": system,
            "type_id": item_id,
            "volume_remain": remain[i],
            "volume_total": total[i],
        }
        ui = u[i]
        if ui[0] < 0.003 * scale:
            row["price"] = str(row["price"])  # numeric string
        if ui[1] < 0.001 * scale:
            del row["volume_remain"]  # missing field
        if ui[2] < 0.0003 * scale:
            row["price"] = -price[i] if ui[2] < 0.00015 * scale else 0  # impossible price
        if ui[3] < 0.0003 * scale:
            row["type_id"] = item_id + 1  # mislabelled row
        if ui[4] < 0.01 * scale:
            row["issued"] = iso[i].replace("T", " ")  # alternative timestamp format
        if ui[5] < 0.002 * scale and "volume_remain" in row:
            row["volume_remain"] = float(row["volume_remain"])  # 12 -> 12.0
        if ui[6] < 0.005 * scale:
            row["is_buy_order_hint"] = "legacy"  # unexpected extra field (must be ignored)
        rows.append(row)
        if ui[7] < 0.002 * scale:
            rows.append(dict(row))  # duplicated row
    return json.dumps(rows, separators=(",", ":"))


def bounty_multiplier(day: int) -> float:
    m = 1.0
    for p in PATCHES:
        if p.bounty_multiplier is not None and p.day <= day:
            m = p.bounty_multiplier
    return m


def _write_wallet_flows(
    cfg: SynthConfig,
    store: RawStore,
    s_idx: int,
    server_id: str,
    day: int,
    until: datetime,
    truth: dict[str, list[Any]],
) -> None:
    """Daily currency faucets (NPC bounties, mission rewards): published the next morning."""
    rng = np.random.default_rng([cfg.seed, s_idx, day, 55])
    d = cfg.start + timedelta(days=day)
    pilots = 4000 * SERVER_SIZE[s_idx]
    bounty = pilots * 30e6 * bounty_multiplier(day) * rng.lognormal(0, 0.08)
    missions = pilots * 8e6 * rng.lognormal(0, 0.08)
    observed = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)
    fetched = observed + timedelta(minutes=31)
    truth.setdefault("faucets", []).append((s_idx, day, bounty + missions))
    if fetched > until:
        return
    body = [
        {"date": d.isoformat(), "ref_type": "bounty_prizes", "amount": round(bounty, 2)},
        {"date": d.isoformat(), "ref_type": "agent_mission_reward", "amount": round(missions, 2)},
    ]
    if rng.random() < 0.01 * cfg.defect_scale:
        body[1]["amount"] = str(body[1]["amount"])  # numeric string
    store.put(
        source=SOURCE,
        kind="wallet_flows",
        day=d,
        key=f"{server_id}_flows",
        body=json.dumps(body),
        fetched_at=fetched,
        observed_at=observed,
        meta={"server_id": server_id},
    )


def _generate_server(args: tuple[Any, ...]) -> tuple[dict[str, int], dict[str, list[Any]]]:
    cfg, s_idx, server_id, items, item_division, cal, n_steps, root, until = args
    store = RawStore(root)
    spd = cfg.snapshots_per_day
    t0 = _epoch(cfg.start)
    stats = {"bundles_written": 0, "bundles_skipped_outage": 0, "bundles_pending_late": 0, "orders": 0}
    truth: dict[str, list[Any]] = {"fills": [], "manip": [], "fair": []}
    region = 20000000 + s_idx
    sims = [
        (item_id, simulate_series(cfg, s_idx, item_id, item_division[item_id], cal[item_id], n_steps, truth))
        for item_id in items
    ]
    for step in range(n_steps):
        responses: list[dict[str, Any]] = []
        for item_id, sim in sims:
            book = next(sim)
            rng = np.random.default_rng([cfg.seed, s_idx, item_id, step, 7])
            page_u = rng.random(2)
            if page_u[0] < 0.003 * cfg.defect_scale:
                responses.append({"type_id": item_id, "page": 1, "status": 502, "body": None})
                continue
            body = render_page(book, item_id, region, rng, cfg.defect_scale)
            if page_u[1] < 0.001 * cfg.defect_scale:
                body = body[: max(1, len(body) // 2)]  # truncated transfer
            responses.append({"type_id": item_id, "page": 1, "status": 200, "body": body})
            stats["orders"] += book.oid.size
        if step % spd == spd - 1:
            _write_wallet_flows(cfg, store, s_idx, server_id, step // spd, until, truth)
        observed = t0 + step * 86400 / spd
        rng = np.random.default_rng([cfg.seed, s_idx, step, 99])
        u = rng.random(3)
        outage_day = s_idx == 2 and step // spd == 170  # one full-day server outage
        if outage_day or u[0] < 0.005 * cfg.defect_scale:
            stats["bundles_skipped_outage"] += 1
            continue
        delay = 60 + rng.uniform(0, 600)
        if u[1] < 0.015 * cfg.defect_scale:
            delay += rng.uniform(1, 3) * 86400  # late arrival
        if observed + delay > until.timestamp():
            stats["bundles_pending_late"] += 1
            continue
        obs_dt = datetime.fromtimestamp(observed, UTC)
        store.put(
            source=SOURCE,
            kind="orders",
            day=obs_dt.date(),
            key=f"{server_id}_{obs_dt:%H%M}",
            body=json.dumps(responses, separators=(",", ":")),
            fetched_at=datetime.fromtimestamp(observed + delay, UTC).replace(microsecond=0),
            observed_at=obs_dt,
            meta={"server_id": server_id, "region_id": region},
        )
        stats["bundles_written"] += 1
    return stats, truth


def generate(
    cfg: SynthConfig,
    store: RawStore,
    reference_dir: Path,
    until: datetime,
    truth_dir: Path | None = None,
    item_division: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Re-simulate from cfg.start and write every payload that has arrived by `until`."""
    cal = load_calibration(reference_dir)
    if item_division is None:
        universe = json.loads((reference_dir / "eve_universe.json").read_text())
        item_division = {i["type_id"]: i["division"] for i in universe["items"]}
    items = [i for i in (cfg.items or sorted(cal)) if i in cal and i in item_division]
    spd = cfg.snapshots_per_day
    t0 = _epoch(cfg.start)
    n_steps = max(0, int((until.timestamp() - t0) // (86400 / spd)) + 1)
    n_days = n_steps // spd + 1
    runs = [
        (cfg, s_idx, server_id, items, item_division, cal, n_steps, store.root, until)
        for s_idx, server_id in enumerate(cfg.server_ids)
    ]
    stats = {"bundles_written": 0, "bundles_skipped_outage": 0, "bundles_pending_late": 0, "orders": 0}
    truth: dict[str, list[Any]] = {"fills": [], "manip": [], "fair": []}
    # Servers are independent simulations: run them in parallel processes (deterministic either way).
    with ProcessPoolExecutor(max_workers=min(len(runs), os.cpu_count() or 1)) as pool:
        for s_stats, s_truth in pool.map(_generate_server, runs):
            for k, v in s_stats.items():
                stats[k] += v
            for k, rows in s_truth.items():
                truth.setdefault(k, []).extend(rows)

    released = [p for p in PATCHES if t0 + (p.day + 11 / 24) * 86400 <= until.timestamp()]
    if released:
        rss = render_rss(cfg, released)
        last = datetime.fromtimestamp(t0 + (released[-1].day + 0.5) * 86400, UTC)
        store.put(
            source=SOURCE,
            kind="patch_rss",
            day=last.date(),
            key="patch_rss",
            body=rss,
            fetched_at=last,
            observed_at=last,
            meta={"url": "synthetic://patch-notes"},
        )
    if truth_dir is not None:
        write_truth(cfg, truth, truth_dir, n_days)
    stats["days"] = n_days
    stats["items"] = len(items)
    return stats


def render_rss(cfg: SynthConfig, patches: list[PatchSpec]) -> str:
    """Same layout as the real EVE feed: one <item> per release with dated 'Patch Notes for' sections."""
    from html import escape

    items = []
    for p in patches:
        d = cfg.start + timedelta(days=p.day)
        section = (
            f"<h2><strong>Patch Notes for {d.isoformat()}.1</strong></h2><p>{escape(p.title)}. {escape(p.notes)}</p>"
        )
        pub = datetime(d.year, d.month, d.day, 11, tzinfo=UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
        items.append(
            f"<item><title>{escape(p.title)}</title><link>synthetic://{p.patch_id}</link>"
            f"<description>{escape(section)}</description><pubDate>{pub}</pubDate></item>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>Synthetic patch-notes'
        "</title>" + "".join(items) + "</channel></rss>"
    )


def write_truth(cfg: SynthConfig, truth: dict[str, list[Any]], truth_dir: Path, n_days: int) -> None:
    truth_dir.mkdir(parents=True, exist_ok=True)
    sid = {i: s for i, s in enumerate(cfg.server_ids)}

    def day(d: int) -> date:
        return cfg.start + timedelta(days=d)

    fills = pl.DataFrame(truth["fills"], schema=["server", "item_id", "day", "qty", "value"], orient="row")
    fills = fills.group_by("server", "item_id", "day").agg(pl.col("qty").sum(), pl.col("value").sum())
    for name, df in {
        "fills": fills,
        "manipulations": pl.DataFrame(
            truth["manip"], schema=["server", "item_id", "day", "kind", "factor"], orient="row"
        ),
        "fair": pl.DataFrame(truth["fair"], schema=["server", "item_id", "day", "fair"], orient="row"),
        "faucets": pl.DataFrame(truth.get("faucets", []), schema=["server", "day", "amount"], orient="row"),
    }.items():
        if df.is_empty():
            continue
        df = df.with_columns(
            server_id=pl.col("server").replace_strict(sid, return_dtype=pl.String),
            day=pl.col("day").map_elements(day, return_dtype=pl.Date),
        ).drop("server")
        df.write_parquet(truth_dir / f"{name}.parquet")
    patches = pl.DataFrame(
        [
            {
                "patch_id": p.patch_id,
                "day": day(p.day),
                "is_major": p.is_major,
                "has_effect": bool(p.effects or p.inflation_per_day),
            }
            for p in PATCHES
            if p.day < n_days
        ]
    )
    patches.write_parquet(truth_dir / "patches.parquet")
    shocks = pl.DataFrame([{"day": day(d), "divisions": list(e.divisions)} for d, e in UNPATCHED_SHOCKS if d < n_days])
    if not shocks.is_empty():
        shocks.write_parquet(truth_dir / "unpatched_shocks.parquet")
