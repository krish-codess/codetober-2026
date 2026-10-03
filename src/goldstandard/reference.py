"""Reference data: worlds, servers, divisions, items and labour activities.

Items come from real ESI type metadata when it has been ingested (names, group, category,
volume); otherwise from the calibration file's names, so an offline checkout still works.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from goldstandard.config import Settings
from goldstandard.raw import RawStore
from goldstandard.sources import synthetic
from goldstandard.sources.eve import load_universe

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


def build_reference(cfg: Settings) -> dict[str, list[dict[str, Any]]]:
    universe = load_universe()
    store = RawStore(cfg.raw_dir)
    meta: dict[int, dict[str, Any]] = {}
    days = store.days("eve", "type_meta")
    if days:
        for ref in store.iter_refs("eve", "type_meta", days[-1]):
            m = json.loads(store.read(ref).body)
            meta[m["type"]["type_id"]] = m
    cal = synthetic.load_calibration(cfg.reference_dir)
    items = []
    for it in universe["items"]:
        tid = it["type_id"]
        m = meta.get(tid, {})
        items.append(
            {
                "item_id": tid,
                "name": m.get("type", {}).get("name") or cal.get(tid, {}).get("name") or f"type {tid}",
                "division_id": it["division"],
                "esi_group": m.get("group", {}).get("name"),
                "esi_category": m.get("category", {}).get("name"),
                "volume_m3": m.get("type", {}).get("volume"),
            }
        )
    sc = synthetic.SynthConfig(start=cfg.synth_start, servers=cfg.synth_servers)
    servers = [
        {"server_id": s["server_id"], "world_id": "eve", "name": s["name"], "region_id": s["region_id"]}
        for s in universe["servers"]
    ]
    servers += [
        {"server_id": sid, "world_id": "synthetic", "name": sid.removeprefix("syn-").title(), "region_id": 20000000 + i}
        for i, sid in enumerate(sc.server_ids)
    ]
    activities = []
    for a in universe["activities"]:
        activities.append(
            {
                "activity_id": a["activity_id"],
                "world_id": "eve",
                "label": a["label"],
                "rates": [{"effective_from": cfg.synth_start, "isk_per_hour": a["isk_per_hour"]}],
                "yields": a["yields"],
            }
        )
    # Synthetic world wages follow its own bounty patches (nominal ISK per hour changes on patch day).
    rates = [{"effective_from": cfg.synth_start, "isk_per_hour": 25e6}]
    for p in synthetic.PATCHES:
        if p.bounty_multiplier is not None:
            rates.append(
                {"effective_from": cfg.synth_start + timedelta(days=p.day), "isk_per_hour": 25e6 * p.bounty_multiplier}
            )
    activities += [
        {
            "activity_id": "syn-mining",
            "world_id": "synthetic",
            "label": "Belt mining (refined minerals)",
            "rates": [{"effective_from": cfg.synth_start, "isk_per_hour": 0.0}],
            "yields": [
                {"type_id": 34, "qty_per_hour": 22000},
                {"type_id": 35, "qty_per_hour": 5500},
                {"type_id": 36, "qty_per_hour": 1400},
            ],
        },
        {
            "activity_id": "syn-ratting",
            "world_id": "synthetic",
            "label": "Ratting (NPC bounties)",
            "rates": rates,
            "yields": [{"type_id": 28668, "qty_per_hour": 8}],
        },
    ]
    return {
        "worlds": [
            {
                "world_id": "eve",
                "name": "EVE Online (Tranquility)",
                "price_source": "trade_history",
                "currency": "ISK",
                "is_synthetic": False,
            },
            {
                "world_id": "synthetic",
                "name": "Synthetic shard",
                "price_source": "snapshots",
                "currency": "ISK",
                "is_synthetic": True,
            },
        ],
        "servers": servers,
        "divisions": [
            {"division_id": d["division"], "label": d["label"], "keywords": DIVISION_KEYWORDS.get(d["division"], [])}
            for d in universe["divisions"]
        ],
        "items": items,
        "activities": activities,
    }
