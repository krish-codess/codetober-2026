"""Decoder rules, checked against published reference sentences and by round trip."""

import random

import pytest

from ghostfleet.nmea import (
    NmeaError,
    Reassembler,
    checksum,
    decode_payload,
    encode_position,
    encode_static,
    parse_sentence,
    to_sentences,
)
from ghostfleet.validate import validate_position

# Reference vectors from the AIVDM/AIVDO protocol documentation and the aishub sample capture.
CLASS_A = "!AIVDM,1,1,,A,13HOI:0P0000VOHLCnHQKwvL05Ip,0*23"
STATIC_1 = "!AIVDM,2,1,1,A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8,0*1C"
STATIC_2 = "!AIVDM,2,2,1,A,88888888880,2*25"


def decode_line(line: str):
    s = parse_sentence(line)
    return decode_payload(s.payload, s.fill)


def test_class_a_position_reference_vector():
    m = decode_line(CLASS_A)
    assert (m["kind"], m["msg_type"], m["mmsi"]) == ("position", 1, 227006760)
    assert m["lat"] == pytest.approx(49.475577, abs=1e-6)
    assert m["lon"] == pytest.approx(0.13138, abs=1e-6)
    assert m["sog"] == 0.0
    assert m["heading"] is None  # 511 on the wire: not available
    assert validate_position({**m, "ts": 0}) is None


def test_multipart_static_reference_vector():
    r = Reassembler()
    first, second = parse_sentence(STATIC_1), parse_sentence(STATIC_2)
    assert r.feed(first, STATIC_1, now=0.0) is None
    payload, fill = r.feed(second, STATIC_2, now=0.1)
    m = decode_payload(payload, fill)
    assert (m["mmsi"], m["imo"], m["callsign"], m["name"]) == (351759000, 9134270, "3FOF8", "EVER DIADEM")
    assert (m["ship_type"], m["destination"], m["draught"]) == (70, "NEW YORK", 12.2)
    assert (m["to_bow"], m["to_stern"]) == (225, 70)
    assert r.take_dropped() == []


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("!AIVDM,1,1,,A,13HOI:0P0000VOHLCnHQKwvL05Ip,0*24", "bad_checksum"),
        ("!AIVDM,1,1,,A,13HOI:0P0000VOHLCnHQKwvL05Ip,0", "malformed_sentence"),
        ("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47", "malformed_sentence"),
        ("", "malformed_sentence"),
        ("!AIVDM,1,1,,A,13HOI:0P00 00VOHLCnHQKwvL05Ip,0*23", "malformed_sentence"),
    ],
)
def test_bad_framing_is_refused_with_a_reason(line, reason):
    with pytest.raises(NmeaError) as e:
        parse_sentence(line)
    assert e.value.reason == reason


def test_fragment_number_beyond_total_is_refused():
    body = "AIVDM,2,3,1,A,88888888880,2"
    with pytest.raises(NmeaError, match="fragment_number_out_of_range"):
        parse_sentence(f"!{body}*{checksum(body):02X}")


def test_tag_block_gives_receiver_time_and_station():
    line = to_sentences(*encode_position(257000001, 60.0, 5.0, 10.0, 90.0, 90, 30), tag_time=1_700_000_000)[0]
    assert parse_sentence(line).tag_time == 1_700_000_000
    tag = "s:2573135,c:1700000000123"
    s = parse_sentence(f"\\{tag}*{checksum(tag):02X}\\{CLASS_A}")
    assert (s.station, s.tag_time) == ("2573135", 1_700_000_000.123)  # milliseconds are recognised
    with pytest.raises(NmeaError, match="bad_tag_checksum"):
        parse_sentence(f"\\{tag}*00\\{CLASS_A}")


def test_position_round_trip_class_a_and_b():
    rng = random.Random(1)
    for _ in range(500):
        lat, lon = rng.uniform(-89, 89), rng.uniform(-179, 179)
        sog, cog, hdg, sec = (
            round(rng.uniform(0, 60), 1),
            round(rng.uniform(0, 359.9), 1),
            rng.randrange(360),
            rng.randrange(60),
        )
        class_b = rng.random() < 0.5
        m = decode_line(to_sentences(*encode_position(244123456, lat, lon, sog, cog, hdg, sec, class_b=class_b))[0])
        assert m["msg_type"] == (18 if class_b else 1)
        assert m["mmsi"] == 244123456
        assert m["lat"] == pytest.approx(lat, abs=1 / 600000)
        assert m["lon"] == pytest.approx(lon, abs=1 / 600000)
        assert (m["sog"], m["cog"], m["heading"], m["second"]) == (pytest.approx(sog), pytest.approx(cog), hdg, sec)


def test_static_round_trip_splits_into_two_sentences():
    lines = to_sentences(*encode_static(538001234, 9134270, "V7AB1", "Nord Trader", 70, 200, 32), seq=3)
    assert len(lines) == 2
    r = Reassembler()
    out = [r.feed(parse_sentence(ln), ln, now=0.0) for ln in lines]
    m = decode_payload(*out[1])
    assert (m["mmsi"], m["imo"], m["callsign"], m["name"], m["ship_type"]) == (
        538001234,
        9134270,
        "V7AB1",
        "NORD TRADER",
        70,
    )
    assert (m["to_bow"] + m["to_stern"], m["to_port"] + m["to_starboard"]) == (200, 32)


def test_sentinels_and_illegal_ancillary_values_become_none_but_keep_the_position():
    m = decode_line(to_sentences(*encode_position(257000001, 60.0, 5.0, None, None, None, 61))[0])
    assert (m["sog"], m["cog"], m["heading"]) == (None, None, None)
    assert (m["lat"], m["second"]) == (pytest.approx(60.0), 61)
    # Heading 400 is neither a heading nor the 511 sentinel. Real transponders send it anyway.
    m = decode_line(to_sentences(*encode_position(257000001, 60.0, 5.0, 5.0, 10.0, 400, 0))[0])
    assert m["heading"] is None and m["lat"] == pytest.approx(60.0)


def test_position_not_available_passes_through_for_validation_to_judge():
    m = decode_line(to_sentences(*encode_position(257000001, 91.0, 181.0, 0.0, None, None, 0))[0])
    assert (m["lat"], m["lon"]) == (91.0, 181.0)


def test_unused_types_are_none_and_impossible_payloads_raise():
    assert decode_payload("402M3b@000Htt<tSF0l4Q@000<0p", 0) is None  # type 4, base station report
    with pytest.raises(NmeaError, match="truncated_payload"):
        decode_payload("13HOI:0P00", 0)
    with pytest.raises(NmeaError, match="empty_payload"):
        decode_payload("", 0)
    with pytest.raises(NmeaError, match="unknown_message_type"):
        decode_payload("w3HOI:0P0000VOHLCnHQKwvL05Ip", 0)  # type 63


def test_decoder_never_raises_anything_but_nmea_error_on_garbage():
    rng = random.Random(2)
    alphabet = "0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVW`abcdefghijklmnopqrstuvw"
    for _ in range(3000):
        payload = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 80)))
        try:
            decode_payload(payload, rng.randrange(6))
        except NmeaError:
            pass


# --- reassembly -----------------------------------------------------------------------------------


def _static(seq: int, mmsi: int = 538001234) -> list[str]:
    return to_sentences(*encode_static(mmsi, 9134270, "V7AB1", "NORD TRADER", 70, 200, 32), seq=seq)


def test_incomplete_fragment_is_dropped_after_timeout_not_before():
    r = Reassembler(timeout_s=5)
    a1, _ = _static(1)
    assert r.feed(parse_sentence(a1), a1, now=100.0, ref="first") is None
    r.expire(104.9)
    assert r.take_dropped() == [] and r.oldest_ref() == "first"
    r.expire(105.1)
    (dropped,) = r.take_dropped()
    assert (dropped.reason, dropped.lines, dropped.ref) == ("incomplete_multipart", [a1], "first")
    assert r.oldest_ref() is None


def test_second_fragment_without_first_is_an_orphan():
    r = Reassembler()
    _, a2 = _static(1)
    assert r.feed(parse_sentence(a2), a2, now=0.0, ref="x") is None
    assert [(d.reason, d.ref) for d in r.take_dropped()] == [("orphan_fragment", "x")]


def test_reused_sequence_id_drops_the_unfinished_message_and_keeps_the_new_one():
    r = Reassembler()
    a1, _ = _static(4, mmsi=538000001)
    b1, b2 = _static(4, mmsi=538000002)
    r.feed(parse_sentence(a1), a1, now=0.0)
    r.feed(parse_sentence(b1), b1, now=1.0)
    done = r.feed(parse_sentence(b2), b2, now=1.1)
    assert decode_payload(*done)["mmsi"] == 538000002
    assert [d.reason for d in r.take_dropped()] == ["incomplete_multipart"]


def test_interleaved_messages_with_different_sequence_ids_both_complete():
    r = Reassembler()
    a1, a2 = _static(1, mmsi=538000001)
    b1, b2 = _static(2, mmsi=538000002)
    got = [r.feed(parse_sentence(ln), ln, now=0.0) for ln in (a1, b1, a2, b2)]
    assert [decode_payload(*g)["mmsi"] for g in got if g] == [538000001, 538000002]
    assert r.take_dropped() == []


def test_pending_groups_are_bounded():
    r = Reassembler(max_groups=4)
    for i in range(10):
        body = f"AIVDM,2,1,{i % 10},A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8,0"
        line = f"!{body}*{checksum(body):02X}"
        r.feed(parse_sentence(line), line, now=float(i))
    assert len(r.take_dropped()) == 6


def test_everything_the_decoder_emits_from_a_healthy_fleet_passes_validation():
    """The decoder and the validator are written apart and must agree on types, not just on ranges."""
    from ghostfleet.simulate import World

    world = World(seed=1, n_ships=80, defects=False)
    decoded = [decode_line(line) for _, line in world.sentences(1_791_500_000, 1_791_500_600) if "AIVDM,1,1," in line]
    positions = [m for m in decoded if m and m["kind"] == "position"]
    assert len(positions) > 200 and any(m["heading"] is not None for m in positions)
    assert [validate_position({**m, "ts": 0}) for m in positions] == [None] * len(positions)
