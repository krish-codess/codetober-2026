"""Spherical geometry on plain floats. PostGIS does the authoritative (spheroidal) distances;
these serve dead reckoning and the simulator, where 0.3% is well inside the noise."""

from __future__ import annotations

from math import asin, atan2, cos, degrees, floor, radians, sin, sqrt

EARTH_M = 6_371_008.8
KNOT_MS = 1852 / 3600


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    a = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_M * asin(min(1.0, sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = radians(lat1), radians(lat2), radians(lon2 - lon1)
    return degrees(atan2(sin(dl) * cos(p2), cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl))) % 360


def destination(lat: float, lon: float, bearing: float, dist_m: float) -> tuple[float, float]:
    """The point `dist_m` away along `bearing`. Longitude is normalised to [-180, 180)."""
    p1, th, d = radians(lat), radians(bearing), dist_m / EARTH_M
    p2 = asin(max(-1.0, min(1.0, sin(p1) * cos(d) + cos(p1) * sin(d) * cos(th))))
    l2 = radians(lon) + atan2(sin(th) * sin(d) * cos(p1), cos(d) - sin(p1) * sin(p2))
    return degrees(p2), (degrees(l2) + 180) % 360 - 180


def cell_of(lat: float, lon: float, cell_deg: float) -> tuple[int, int]:
    """Grid cell (cx, cy) containing a point. Must match the SQL in coverage.refresh()."""
    return floor(lon / cell_deg), floor(lat / cell_deg)
