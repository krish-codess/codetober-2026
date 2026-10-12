"""Profile a raw NMEA capture: what is actually in the feed, and which assumptions it breaks."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .nmea import NmeaError, Reassembler, decode_payload, parse_sentence
from .validate import validate_position


def _pct(n: int, total: int) -> str:
    return f"{n:,} ({100 * n / total:.2f}%)" if total else "0"


def profile(path: Path) -> str:
    lines = [ln for ln in path.read_text(encoding="ascii", errors="replace").splitlines() if ln and ln[0] != "#"]
    framing: Counter[str] = Counter()
    types: Counter[str] = Counter()
    nulls: Counter[str] = Counter()
    invalid: Counter[str] = Counter()
    mmsi_class: Counter[str] = Counter()
    vessels: set[int] = set()
    sog: list[float] = []
    seen: Counter[str] = Counter()
    positions = statics = 0
    reasm = Reassembler(timeout_s=5.0)
    for i, raw in enumerate(lines):
        # Captures without receive times: treat line order as time, 100 lines per "second".
        if "\t" in raw:
            raw = raw.split("\t", 1)[1]
        seen[raw] += 1
        try:
            s = parse_sentence(raw)
        except NmeaError as e:
            framing[e.reason] += 1
            continue
        framing["multipart_fragment" if s.total > 1 else "single_part"] += 1
        reasm.expire(i / 100)
        done = reasm.feed(s, raw, i / 100)
        if done is None:
            continue
        try:
            msg = decode_payload(*done)
        except NmeaError as e:
            framing[f"decode:{e.reason}"] += 1
            continue
        if msg is None:
            types["other (unused types)"] += 1
            continue
        types[f"type {msg['msg_type']}"] += 1
        if msg["kind"] == "static":
            statics += 1
            for k in ("imo", "callsign", "name", "destination"):
                if k in msg and msg[k] is None:
                    nulls[f"static.{k}"] += 1
            continue
        positions += 1
        vessels.add(msg["mmsi"])
        m = msg["mmsi"]
        mmsi_class["ship (MID 2xx-7xx)" if 200_000_000 <= m <= 799_999_999 else f"other ({str(m).zfill(9)[:2]}x)"] += 1
        for k in ("sog", "cog", "heading"):
            if msg[k] is None:
                nulls[f"position.{k}"] += 1
        if msg["second"] is None or msg["second"] > 59:
            nulls["position.second (>59: no UTC second)"] += 1
        if msg["sog"] is not None:
            sog.append(msg["sog"])
        reason = validate_position({**msg, "ts": 0})
        if reason:
            invalid[reason] += 1
    reasm.expire(float("inf"))
    for d in reasm.take_dropped():
        framing[f"dropped:{d.reason}"] += 1
    dupes = sum(c - 1 for c in seen.values() if c > 1)
    sog.sort()

    def q(p: float) -> float:
        return sog[min(len(sog) - 1, int(p * len(sog)))] if sog else 0.0

    def table(title: str, counter: Counter[str], total: int) -> list[str]:
        return [
            f"\n### {title}\n",
            "| value | count |",
            "|---|---|",
            *(f"| {k} | {_pct(v, total)} |" for k, v in counter.most_common()),
        ]

    out = [
        f"# Data profile: `{path.name}`\n",
        f"- sentences: {len(lines):,}",
        f"- byte-identical repeated sentences: {_pct(dupes, len(lines))}",
        f"- decoded position reports: {positions:,} from {len(vessels):,} distinct MMSIs",
        f"- decoded static reports: {statics:,}",
        f"- speed over ground, knots: p50 {q(0.5)}, p90 {q(0.9)}, p99 {q(0.99)}, max {sog[-1] if sog else 0}",
        f"- stationary (sog < 0.5 kn): {_pct(sum(1 for v in sog if v < 0.5), len(sog))}",
        *table("Framing", framing, len(lines)),
        *table("Message types (complete messages)", types, sum(types.values())),
        *table("MMSI class of position reports", mmsi_class, positions),
        *table("Fields carrying the 'not available' sentinel", nulls, positions),
        *table("Position reports failing validation (quarantined)", invalid, positions),
    ]
    return "\n".join(out) + "\n"
