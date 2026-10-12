"""Boundary validation and the two small derivations every message needs: its timestamp and its flag."""

from __future__ import annotations

import csv
from functools import cache
from importlib import resources
from typing import Any

# How far ahead of our own clock a transmitter's second-of-minute may plausibly be.
CLOCK_SKEW_S = 5
# A position stamped further into the future than this is a clock fault, not a late arrival.
MAX_FUTURE_S = 120


def position_time(received_at: float, second: int | None) -> int:
    """The UTC second a position was fixed, reconstructed from the receive time.

    AIS position reports carry only the second-of-minute of the fix. Two receivers that hear the
    same transmission stamp it with different receive times but must produce the same
    (mmsi, timestamp) key, so the key is the latest instant at or just before the receive time
    whose seconds match. Anything delayed by more than 60 - CLOCK_SKEW_S seconds aliases by a
    minute; feeds that slow need a provider timestamp instead (AISStream and tag blocks give one).
    """
    base = int(received_at)
    if second is None or not 0 <= second <= 59:
        return base
    delta = (base - second) % 60
    if delta > 60 - CLOCK_SKEW_S:
        delta -= 60
    return base - delta


def is_vessel_mmsi(mmsi: Any) -> bool:
    # MID 2xx-7xx are ship stations. Everything else (base stations 00x, aircraft 111, aids 99x,
    # handhelds 8x, SARTs 97x) is not a vessel that can "go dark".
    return isinstance(mmsi, int) and not isinstance(mmsi, bool) and 200_000_000 <= mmsi <= 799_999_999


def _number(value: Any) -> float | None:
    """The value as a float if it is a real, finite-or-not number (and not NaN, not a bool), else None."""
    ok = isinstance(value, int | float) and not isinstance(value, bool) and value == value  # NaN != NaN
    return float(value) if ok else None


def validate_position(msg: dict[str, Any], now: float | None = None) -> str | None:
    """None if the position is storable, else a stable reason code for the quarantine."""
    if not is_vessel_mmsi(msg.get("mmsi")):
        return "non_vessel_mmsi"
    lat, lon = _number(msg.get("lat")), _number(msg.get("lon"))
    if lat is None or lon is None:
        return "missing_position"
    if lat == 91 or lon == 181:
        return "position_unavailable"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "position_out_of_range"
    if abs(lat) < 0.001 and abs(lon) < 0.001:
        return "null_island"
    # Optional fields: absent is fine, present must be a legal value of the right type.
    for key, top, whole in (
        ("sog", 102.2, False),
        ("cog", 359.9, False),
        ("heading", 359, True),
        ("nav_status", 15, True),
    ):
        raw = msg.get(key)
        v = _number(raw)
        if raw is not None and (v is None or not 0 <= v <= top or (whole and not isinstance(raw, int))):
            return f"{key}_out_of_range"
    ts = _number(msg.get("ts"))
    if ts is None or ts < 0:
        return "missing_timestamp"
    if now is not None and ts > now + MAX_FUTURE_S:
        return "timestamp_in_future"
    return None


@cache
def mid_table() -> dict[int, tuple[str, str]]:
    """ITU Maritime Identification Digits: MID -> (ISO 3166 alpha-2, country)."""
    with resources.files("ghostfleet").joinpath("data/mid.csv").open(encoding="utf-8") as f:
        return {int(r["mid"]): (r["iso2"], r["country"]) for r in csv.DictReader(f)}


def flag_of(mmsi: int) -> str | None:
    entry = mid_table().get(mmsi // 1_000_000)
    return entry[0] if entry else None
