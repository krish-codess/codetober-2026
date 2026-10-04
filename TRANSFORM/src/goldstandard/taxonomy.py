"""Dependency-free domain constants shared by the pipeline and the API (the API image carries no
polars/numpy, so nothing it imports may pull them in - see tests/test_api_image.py)."""

from __future__ import annotations

METHOD_VERSION = "gs-1.0"
BASKET_VALUE = 1_000_000_000.0  # the reference basket costs 1B ISK at base prices (roughly a month of play)

# Keywords used to tag patch notes with the CPI divisions they plausibly affect (see analytics.attribute).
DIVISION_KEYWORDS: dict[str, list[str]] = {
    "minerals": ["mining", "ore ", "ores", "mineral", "reprocess", "veldspar", "scordite", "asteroid", "tritanium"],
    "fuel": ["fuel", "ice ", "ice product", "isotope", "heavy water", "liquid ozone", "strontium"],
    "industrial": ["moon", "planetary", "reaction", "manufactur", "blueprint", "industry"],
    "ships": ["ship hull", "hull", "frigate", "cruiser", "battleship", "destroyer", "mining barge", "ship production"],
    "equipment": ["module", "drone", "weapon", "fitting"],
    "consumables": ["ammunition", "ammo", "charge", "missile", "nanite", "repair paste"],
    "services": ["plex", "skill injector", "injector", "extractor", "omega", "new eden store", "store"],
}


def tag_divisions(text: str) -> list[str]:
    """Divisions mentioned by a patch, most-mentioned first (ties alphabetical)."""
    t = text.lower()
    hits = {d: sum(t.count(w) for w in words) for d, words in DIVISION_KEYWORDS.items()}
    return [d for d, n in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0])) if n > 0]
