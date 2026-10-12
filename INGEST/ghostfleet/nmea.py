"""NMEA 0183 AIVDM framing and AIS (ITU-R M.1371) payload decoding.

Pure functions and one small state machine (the reassembler). Nothing here touches the network,
the clock or a database, so every rule is unit-testable and replaying an archive is deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import reduce
from typing import Any

Message = dict[str, Any]

# Optional NMEA 4.10 tag block (\s:station,c:unixtime*hh\), then the sentence itself.
_SENTENCE = re.compile(
    r"^(?:\\(?P<tag>[^\\]*)\\)?"
    r"!(?P<body>[A-Z]{2}VD[MO],(?P<total>[1-9]),(?P<num>[1-9]),(?P<seq>[0-9]?),(?P<chan>[AB12]?),"
    r"(?P<payload>[0-W`-w]*),(?P<fill>[0-5]))\*(?P<ck>[0-9A-Fa-f]{2})\s*$"
)

POSITION_TYPES = frozenset({1, 2, 3, 18, 19, 27})
STATIC_TYPES = frozenset({5, 24})
# Shortest payload we accept per type, in bits. Type 5 is 424 by the standard but transponders
# that send 422 or 420 are common enough in the wild that rejecting them would lose real vessels.
_MIN_BITS = {1: 168, 2: 168, 3: 168, 18: 168, 19: 312, 27: 96, 5: 420, 24: 160}


class NmeaError(ValueError):
    """A sentence or payload we refuse. `reason` is a stable code used as the quarantine reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Sentence:
    total: int
    num: int
    seq: str
    channel: str
    payload: str
    fill: int
    tag_time: float | None  # receiver timestamp from the tag block, unix seconds
    station: str | None


def checksum(text: str) -> int:
    return reduce(lambda acc, ch: acc ^ ord(ch), text, 0)


def parse_sentence(line: str) -> Sentence:
    m = _SENTENCE.match(line.strip())
    if m is None:
        raise NmeaError("malformed_sentence")
    if checksum(m["body"]) != int(m["ck"], 16):
        raise NmeaError("bad_checksum")
    total, num = int(m["total"]), int(m["num"])
    if num > total:
        raise NmeaError("fragment_number_out_of_range")
    tag_time: float | None = None
    station: str | None = None
    if m["tag"]:
        body, _, ck = m["tag"].partition("*")
        if len(ck) != 2 or checksum(body) != int(ck, 16):
            raise NmeaError("bad_tag_checksum")
        for field in body.split(","):
            key, _, value = field.partition(":")
            if key == "c" and value.isdigit():
                # Seconds by the standard; some relays send milliseconds.
                tag_time = int(value) / 1000 if len(value) > 11 else float(value)
            elif key == "s":
                station = value
    return Sentence(total, num, m["seq"], m["chan"], m["payload"], int(m["fill"]), tag_time, station)


@dataclass(slots=True)
class _Group:
    parts: list[Sentence]
    lines: list[str]
    started: float
    ref: Any


@dataclass(frozen=True, slots=True)
class Dropped:
    reason: str
    lines: list[str]
    ref: Any = None  # the caller's token for the group's first fragment


class Reassembler:
    """Joins multi-part sentences. Incomplete groups are handed back, never silently forgotten.

    Fragments of one message share (sequence id, channel, fragment count) and arrive in order.
    `ref` is an opaque caller token for the first fragment of a group; `oldest_ref()` lets the
    caller checkpoint no further than the oldest fragment still waiting for its siblings.
    """

    def __init__(self, timeout_s: float = 5.0, max_groups: int = 4096) -> None:
        self.timeout_s = timeout_s
        self.max_groups = max_groups
        self._groups: dict[tuple[str, str, int], _Group] = {}
        self._dropped: list[Dropped] = []

    def feed(self, s: Sentence, line: str, now: float, ref: Any = None) -> tuple[str, int] | None:
        """Returns (payload, fill_bits) once a message is complete, else None.

        Anything dropped along the way is available from `take_dropped()`.
        """
        if s.total == 1:
            return s.payload, s.fill
        key = (s.seq, s.channel, s.total)
        group = self._groups.get(key)
        if s.num == 1:
            if group is not None:  # a new message reused the sequence id before the old one finished
                self._dropped.append(Dropped("incomplete_multipart", group.lines, group.ref))
            if len(self._groups) >= self.max_groups and key not in self._groups:
                old = self._groups.pop(min(self._groups, key=lambda k: self._groups[k].started))
                self._dropped.append(Dropped("incomplete_multipart", old.lines, old.ref))
            self._groups[key] = _Group([s], [line], now, ref)
            return None
        if group is None or len(group.parts) != s.num - 1:
            if group is not None:
                del self._groups[key]
                self._dropped.append(Dropped("fragment_out_of_order", [*group.lines, line], group.ref))
            else:
                self._dropped.append(Dropped("orphan_fragment", [line], ref))
            return None
        group.parts.append(s)
        group.lines.append(line)
        if s.num < s.total:
            return None
        del self._groups[key]
        return "".join(p.payload for p in group.parts), s.fill

    def expire(self, now: float) -> None:
        for key in [k for k, g in self._groups.items() if now - g.started > self.timeout_s]:
            old = self._groups.pop(key)
            self._dropped.append(Dropped("incomplete_multipart", old.lines, old.ref))

    def take_dropped(self) -> list[Dropped]:
        out, self._dropped = self._dropped, []
        return out

    def oldest_ref(self) -> Any:
        if not self._groups:
            return None
        return min(self._groups.values(), key=lambda g: g.started).ref


def _unarmor(payload: str, fill: int) -> tuple[int, int]:
    """Payload characters to one big integer plus its length in bits."""
    n = 0
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        n = (n << 6) | v
    nbits = 6 * len(payload) - fill
    return (n >> fill if fill else n), nbits


class _Bits:
    __slots__ = ("n", "nbits")

    def __init__(self, n: int, nbits: int, pad_to: int) -> None:
        # Right-pad short-but-acceptable payloads with zeros so field offsets stay valid.
        if nbits < pad_to:
            n <<= pad_to - nbits
            nbits = pad_to
        self.n, self.nbits = n, nbits

    def u(self, start: int, width: int) -> int:
        return (self.n >> (self.nbits - start - width)) & ((1 << width) - 1)

    def s(self, start: int, width: int) -> int:
        v = self.u(start, width)
        return v - (1 << width) if v >> (width - 1) else v

    def text(self, start: int, width: int) -> str | None:
        chars = []
        for i in range(start, start + width, 6):
            v = self.u(i, 6)
            chars.append(chr(v + 64 if v < 32 else v))
        out = "".join(chars).split("@", 1)[0].strip()
        return out or None


def _nullable(value: int, limit: int, scale: int = 1) -> float | None:
    """Raw field to a number, or None at or beyond `limit`.

    The limit is the first value the standard does not define as a measurement: the "not available"
    sentinel and, above or below it, values a healthy transponder never sends (heading 360-510 turns
    up in real feeds). A broken ancillary field should not cost us the position it rode in with.
    """
    if value >= limit:
        return None
    return value if scale == 1 else value / scale  # whole-unit fields (heading) stay integers


def decode_payload(payload: str, fill: int) -> Message | None:
    """Decode one complete armored payload.

    Returns a position or static message, or None for a well-formed message of a type this system
    does not use (base stations, binary broadcasts, aids to navigation...). Raises NmeaError for
    payloads that claim a supported type but cannot be one.

    "Not available" sentinels for speed, course and heading become None. Latitude 91 / longitude 181
    are passed through untouched: whether a position report without a position is acceptable is a
    validation decision, not a decoding one.
    """
    if not payload:
        raise NmeaError("empty_payload")
    n, nbits = _unarmor(payload, fill)
    if nbits < 38:
        raise NmeaError("truncated_payload", f"{nbits} bits")
    msg_type = n >> (nbits - 6)
    need = _MIN_BITS.get(msg_type)
    if need is None:
        if not 1 <= msg_type <= 27:
            raise NmeaError("unknown_message_type", str(msg_type))
        return None
    if nbits < need:
        raise NmeaError("truncated_payload", f"type {msg_type} needs {need} bits, got {nbits}")
    b = _Bits(n, nbits, {5: 424, 24: 168}.get(msg_type, need))
    mmsi = b.u(8, 30)

    if msg_type in (1, 2, 3):
        return {
            "kind": "position", "msg_type": msg_type, "mmsi": mmsi, "nav_status": b.u(38, 4),
            "sog": _nullable(b.u(50, 10), 1023, 10), "lon": b.s(61, 28) / 600000, "lat": b.s(89, 27) / 600000,
            "cog": _nullable(b.u(116, 12), 3600, 10), "heading": _nullable(b.u(128, 9), 360), "second": b.u(137, 6),
        }  # fmt: skip
    if msg_type in (18, 19):
        msg: Message = {
            "kind": "position", "msg_type": msg_type, "mmsi": mmsi, "nav_status": None,
            "sog": _nullable(b.u(46, 10), 1023, 10), "lon": b.s(57, 28) / 600000, "lat": b.s(85, 27) / 600000,
            "cog": _nullable(b.u(112, 12), 3600, 10), "heading": _nullable(b.u(124, 9), 360), "second": b.u(133, 6),
        }  # fmt: skip
        if msg_type == 19:
            msg["static"] = {
                "kind": "static", "msg_type": 19, "mmsi": mmsi, "name": b.text(143, 120), "ship_type": b.u(263, 8),
                "to_bow": b.u(271, 9), "to_stern": b.u(280, 9), "to_port": b.u(289, 6), "to_starboard": b.u(295, 6),
            }  # fmt: skip
        return msg
    if msg_type == 27:  # long-range broadcast: coarse position, whole knots and degrees, no second-of-minute
        return {
            "kind": "position", "msg_type": 27, "mmsi": mmsi, "nav_status": b.u(40, 4),
            "lon": b.s(44, 18) / 600, "lat": b.s(62, 17) / 600, "sog": _nullable(b.u(79, 6), 63),
            "cog": _nullable(b.u(85, 9), 360), "heading": None, "second": None,
        }  # fmt: skip
    if msg_type == 5:
        return {
            "kind": "static", "msg_type": 5, "mmsi": mmsi, "imo": b.u(40, 30) or None, "callsign": b.text(70, 42),
            "name": b.text(112, 120), "ship_type": b.u(232, 8), "to_bow": b.u(240, 9), "to_stern": b.u(249, 9),
            "to_port": b.u(258, 6), "to_starboard": b.u(264, 6), "draught": b.u(294, 8) / 10 or None,
            "destination": b.text(302, 120),
        }  # fmt: skip
    # Type 24 comes in two halves that share nothing but the MMSI.
    part = b.u(38, 2)
    if part == 0:
        return {"kind": "static", "msg_type": 24, "mmsi": mmsi, "name": b.text(40, 120)}
    if part == 1:
        return {
            "kind": "static", "msg_type": 24, "mmsi": mmsi, "ship_type": b.u(40, 8), "callsign": b.text(90, 42),
            "to_bow": b.u(132, 9), "to_stern": b.u(141, 9), "to_port": b.u(150, 6), "to_starboard": b.u(156, 6),
        }  # fmt: skip
    raise NmeaError("bad_type24_part", str(part))


# --- encoding: used by the simulator, and by the tests to round-trip the decoder ---------------


def _armor(fields: list[tuple[int, int]]) -> tuple[str, int]:
    """(value, width) fields to an armored payload and its fill-bit count."""
    n = nbits = 0
    for value, width in fields:
        n = (n << width) | (value & ((1 << width) - 1))
        nbits += width
    fill = -nbits % 6
    n <<= fill
    chars = []
    for shift in range(nbits + fill - 6, -1, -6):
        v = (n >> shift) & 63
        chars.append(chr(v + 48 if v < 40 else v + 56))
    return "".join(chars), fill


def _text_fields(text: str, chars: int) -> list[tuple[int, int]]:
    padded = text.upper()[:chars].ljust(chars, "@")
    return [((ord(c) - 64 if ord(c) >= 64 else ord(c)) & 63, 6) for c in padded]


def encode_position(
    mmsi: int, lat: float, lon: float, sog: float | None, cog: float | None, heading: int | None, second: int,
    *, class_b: bool = False, nav_status: int = 0,
) -> tuple[str, int]:  # fmt: skip
    sog_raw = 1023 if sog is None else round(sog * 10)
    cog_raw = 3600 if cog is None else round(cog * 10) % 3600
    hdg = 511 if heading is None else heading
    lon_raw, lat_raw = round(lon * 600000), round(lat * 600000)
    if class_b:
        return _armor([(18, 6), (0, 2), (mmsi, 30), (0, 8), (sog_raw, 10), (0, 1), (lon_raw, 28), (lat_raw, 27),
                       (cog_raw, 12), (hdg, 9), (second, 6), (0, 29)])  # fmt: skip
    return _armor([(1, 6), (0, 2), (mmsi, 30), (nav_status, 4), (128, 8), (sog_raw, 10), (0, 1), (lon_raw, 28),
                   (lat_raw, 27), (cog_raw, 12), (hdg, 9), (second, 6), (0, 25)])  # fmt: skip


def encode_static(
    mmsi: int, imo: int, callsign: str, name: str, ship_type: int, length: int, beam: int
) -> tuple[str, int]:
    bow, port = length // 2, beam // 2
    return _armor([
        (5, 6), (0, 2), (mmsi, 30), (0, 2), (imo, 30), *_text_fields(callsign, 7), *_text_fields(name, 20),
        (ship_type, 8), (bow, 9), (length - bow, 9), (port, 6), (beam - port, 6), (1, 4), (0, 20), (0, 8),
        *_text_fields("", 20), (0, 2),
    ])  # fmt: skip


def to_sentences(payload: str, fill: int, *, seq: int = 0, channel: str = "A", tag_time: int | None = None,
                 max_chars: int = 60) -> list[str]:  # fmt: skip
    chunks = [payload[i : i + max_chars] for i in range(0, len(payload), max_chars)]
    out = []
    for i, chunk in enumerate(chunks, 1):
        seq_field = str(seq % 10) if len(chunks) > 1 else ""
        body = f"AIVDM,{len(chunks)},{i},{seq_field},{channel},{chunk},{fill if i == len(chunks) else 0}"
        line = f"!{body}*{checksum(body):02X}"
        if tag_time is not None:
            tag = f"c:{tag_time}"
            line = f"\\{tag}*{checksum(tag):02X}\\{line}"
        out.append(line)
    return out
