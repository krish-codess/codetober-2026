"""Business rules that need no infrastructure: timestamps, validation, backoff, scoring, the simulator."""

import dataclasses
import random
from datetime import UTC, datetime

import pytest

from ghostfleet.collectors import Backoff, aisstream_message
from ghostfleet.config import Settings, _sources
from ghostfleet.detect import Gap, assess, projected, reliability
from ghostfleet.geo import cell_of, destination, distance_m
from ghostfleet.simulate import World, identity_check
from ghostfleet.sink import _clean_static, valid_imo
from ghostfleet.validate import flag_of, is_vessel_mmsi, position_time, validate_position

S = Settings()
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

# --- the idempotency key ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("received", "second", "expected"),
    [
        (1000.0, 40, 1000),  # 1000 % 60 == 40: fixed this very second
        (1003.7, 40, 1000),  # heard 3.7 s later by a slower receiver: same key
        (1039.0, 40, 1000),  # 39 s late: still the same fix
        (1054.0, 40, 1000),  # at the edge of what is treated as "late"
        (998.0, 40, 1000),  # transmitter clock 2 s ahead of ours: the fix is in our near future
        (1000.9, 60, 1000),  # second 60: no UTC second available, fall back to receive time
        (1000.9, None, 1000),
        (1020.0, 59, 1019),
    ],
)
def test_position_time_reconstructs_the_fix_second(received, second, expected):
    assert position_time(received, second) == expected


def test_two_receivers_hearing_one_transmission_agree_on_the_key():
    rng = random.Random(3)
    for _ in range(2000):
        fix = rng.randrange(1_700_000_000, 1_800_000_000)
        a, b = fix + rng.uniform(0, 50), fix + rng.uniform(0, 50)
        assert position_time(a, fix % 60) == position_time(b, fix % 60) == fix


# --- boundary validation ---------------------------------------------------------------------------

GOOD = {
    "mmsi": 257000001,
    "lat": 60.0,
    "lon": 5.0,
    "sog": 10.0,
    "cog": 90.0,
    "heading": 90,
    "nav_status": 0,
    "ts": 1_700_000_000,
}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({}, None),
        ({"sog": None, "cog": None, "heading": None, "nav_status": None}, None),
        ({"mmsi": 2573100}, "non_vessel_mmsi"),
        ({"mmsi": 992570001}, "non_vessel_mmsi"),
        ({"mmsi": "257000001"}, "non_vessel_mmsi"),
        ({"mmsi": True}, "non_vessel_mmsi"),
        ({"lat": 91, "lon": 181}, "position_unavailable"),
        ({"lat": 95.0}, "position_out_of_range"),
        ({"lon": -200.0}, "position_out_of_range"),
        ({"lat": 0.0, "lon": 0.0}, "null_island"),
        ({"lat": None}, "missing_position"),
        ({"lat": float("nan")}, "missing_position"),
        ({"lat": "60"}, "missing_position"),
        ({"sog": 150.0}, "sog_out_of_range"),
        ({"sog": "fast"}, "sog_out_of_range"),
        ({"cog": 360.0}, "cog_out_of_range"),
        ({"sog": 102.2}, None),
        ({"sog": 102.3}, "sog_out_of_range"),
        ({"heading": 90.5}, "heading_out_of_range"),
        ({"heading": 400}, "heading_out_of_range"),
        ({"nav_status": 16}, "nav_status_out_of_range"),
        ({"ts": None}, "missing_timestamp"),
        ({"ts": 1_700_000_000 + 121}, "timestamp_in_future"),
        ({"ts": 1_700_000_000 + 119}, None),
    ],
)
def test_validate_position(change, reason):
    assert validate_position({**GOOD, **change}, now=1_700_000_000) == reason


def test_mmsi_class_flag_and_imo_check_digit():
    assert is_vessel_mmsi(200000000) and is_vessel_mmsi(799999999)
    assert not is_vessel_mmsi(199999999) and not is_vessel_mmsi(800000000)
    assert (flag_of(257000001), flag_of(351759000), flag_of(799000001)) == ("NO", "PA", None)
    assert valid_imo(9134270) and valid_imo(9074729)
    assert not valid_imo(9134271) and not valid_imo(123) and not valid_imo(None) and not valid_imo("9134270")


def test_static_cleaning_drops_garbage_rather_than_storing_it():
    c = _clean_static(
        {
            "imo": 1234568,
            "name": "  ",
            "callsign": "LAB1",
            "ship_type": 0,
            "to_bow": 0,
            "to_stern": 0,
            "to_port": 5,
            "to_starboard": 6,
            "draught": 0,
        }
    )
    assert c == {
        "imo": None,
        "name": None,
        "callsign": "LAB1",
        "ship_type": None,
        "length_m": None,
        "beam_m": 11,
        "draught_m": None,
        "destination": None,
    }


def test_sources_setting_is_validated():
    assert _sources("a=tcp://h:1, b=wss://x/y") == {"a": "tcp://h:1", "b": "wss://x/y"}
    for bad in ("a", "a=http://h", "a b=tcp://h:1", "=tcp://h:1"):
        with pytest.raises(ValueError, match="GHOST_SOURCES"):
            _sources(bad)


# --- reconnect backoff -------------------------------------------------------------------------------


def test_backoff_is_exponential_capped_and_fully_jittered():
    top = Backoff(base=1, cap=60, rng=lambda: 1.0)  # the ceiling of each draw
    assert [top.next() for _ in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    top.reset()
    assert top.next() == 1
    assert Backoff(1, 60, rng=lambda: 0.0).next() == 0  # full jitter reaches down to zero
    rng = random.Random(4)
    b = Backoff(1, 60, rng=rng.random)
    draws = [b.next() for _ in range(200)]
    assert all(0 <= d <= 60 for d in draws)
    assert len({round(d, 6) for d in draws[10:]}) > 150  # spread out, not a thundering herd


# --- AISStream mapping ---------------------------------------------------------------------------------


def test_aisstream_position_maps_to_the_same_shape_and_key_as_nmea():
    doc = {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 259000420, "time_utc": "2022-12-29 18:22:32.318353 +0000 UTC"},
        "Message": {
            "PositionReport": {
                "UserID": 259000420,
                "Latitude": 66.02695,
                "Longitude": 12.2538,
                "Sog": 0,
                "Cog": 360,
                "TrueHeading": 511,
                "NavigationalStatus": 15,
                "Timestamp": 31,
            }
        },
    }
    m = aisstream_message(doc)
    assert (m["kind"], m["mmsi"], m["lat"], m["heading"], m["cog"]) == ("position", 259000420, 66.02695, None, None)
    assert m["ts"] == int(datetime(2022, 12, 29, 18, 22, 31, tzinfo=UTC).timestamp())
    assert validate_position({**m, "nav_status": 15}) is None
    assert aisstream_message({"MessageType": "BaseStationReport", "Message": {"BaseStationReport": {}}}) is None
    with pytest.raises(KeyError):
        aisstream_message({"MessageType": "PositionReport", "Message": {}})


# --- geometry --------------------------------------------------------------------------------------------


def test_geometry_helpers():
    assert distance_m(60, 5, 60, 6) == pytest.approx(55_597, rel=0.005)
    lat, lon = destination(60, 5, 90, 55_597)
    assert (lat, lon) == (pytest.approx(60, abs=0.01), pytest.approx(6, abs=0.01))
    assert destination(0, 179.9, 90, 50_000)[1] < -179  # wraps across the antimeridian
    assert cell_of(60.1, 5.3, 0.25) == (21, 240) and cell_of(-0.1, -0.1, 0.25) == (-1, -1)
    assert projected(60, 5, None, 90, 900) == (60, 5) and projected(60, 5, 10, None, 900) == (60, 5)
    ahead, behind = projected(60, 5, 12, 90, 1800), projected(60, 5, 12, 90, -1800)
    assert ahead[1] > 5 > behind[1]
    assert distance_m(60, 5, *ahead) == pytest.approx(6 * 1852, rel=0.01)  # 12 kn for half an hour


def test_reliability_is_the_worst_cell_along_the_track():
    cov = {cell_of(60.1, 5.0, 0.25): 1.0, cell_of(60.1, 5.3, 0.25): 0.4}
    assert reliability(cov, 60.1, 5.0, 0, None, +1, 0.25) == 1.0  # not moving: only its own cell counts
    assert reliability(cov, 60.1, 5.0, 20, 90, +1, 0.25) == 0.4  # steaming east into poor reception
    assert reliability(cov, 60.1, 5.0, 20, 270, +1, 0.25) == 0.0  # steaming west off the map
    assert reliability(cov, 60.1, 5.45, 20, 90, -1, 0.25) == 0.4  # arrived from the west: looks back along its wake


# --- classification and scoring ------------------------------------------------------------------------


def gap(**kw):
    base = dict(
        mmsi=257000001,
        t0=T0,
        lat0=60.0,
        lon0=5.0,
        sog0=12.0,
        cog0=90.0,
        duration_s=3600.0,
        t1=T0,
        lat1=60.0,
        lon1=5.4,
        sog1=12.0,
        cog1=90.0,
        dist_m=22_000.0,
    )
    return Gap(**{**base, **kw})


def test_silence_in_covered_water_with_neighbours_heard_is_dark():
    a = assess(gap(), 0.95, 0.9, 4, S)
    assert (a.classification, a.impossible_speed) == ("dark", False)
    assert a.score == pytest.approx(45 + 10 * 3600 / 43200 + 8 + 5, abs=0.1)
    assert any("4 other vessels heard nearby" in r for r in a.reasons)


@pytest.mark.parametrize(
    ("rel0", "rel1", "neighbours", "phrase"),
    [
        (0.3, 0.95, 4, "was leaving reliable coverage"),
        (0.95, 0.2, 4, "returned from outside reliable coverage"),
        (0.95, 0.95, 0, "receiver may have been down"),
    ],
)
def test_any_missing_piece_of_evidence_makes_it_a_coverage_gap(rel0, rel1, neighbours, phrase):
    a = assess(gap(), rel0, rel1, neighbours, S)
    assert (a.classification, a.score) == ("coverage_gap", 5.0)
    assert any(phrase in r for r in a.reasons)


def test_open_event_is_judged_on_the_disappearance_alone():
    still_silent = gap(t1=None, lat1=None, lon1=None, sog1=None, cog1=None, dist_m=None)
    a = assess(still_silent, 0.95, None, 2, S)
    assert (a.classification, a.implied_speed_kn, a.impossible_speed) == ("dark", None, False)
    assert "not yet heard again" in a.reasons[0]


def test_impossible_reappearance_is_flagged_and_outranks_everything():
    far = gap(dist_m=1_852_000.0)  # 1000 nm in an hour
    a = assess(far, 0.95, 0.95, 4, S)
    assert a.impossible_speed and a.implied_speed_kn == pytest.approx(1000)
    assert a.score > assess(gap(), 0.95, 0.95, 4, S).score + 30
    assert any("needs 1000 kn, limit 50 kn" in r for r in a.reasons)
    # Suspicious even where coverage cannot vouch for the silence itself.
    b = assess(far, 0.1, 0.1, 0, S)
    assert (b.classification, b.impossible_speed) == ("coverage_gap", True) and b.score > 35


def test_speed_limit_boundaries():
    hour_at = lambda kn: gap(dist_m=kn * 1852.0)  # noqa: E731
    assert not assess(hour_at(49.9), 1, 1, 1, S).impossible_speed
    assert assess(hour_at(50.1), 1, 1, 1, S).impossible_speed
    # GPS jitter: 5 nm in 30 seconds is 600 kn, but too short a hop to call a teleport.
    assert not assess(gap(duration_s=30.0, dist_m=5 * 1852.0), 1, 1, 1, S).impossible_speed


def test_brief_jump_is_a_position_jump_not_a_dark_period():
    a = assess(gap(duration_s=120.0, dist_m=900_000.0), 1.0, 1.0, 5, S)
    assert (a.classification, a.impossible_speed) == ("position_jump", True)


def test_stationary_vessel_gets_the_long_leash():
    moored = gap(sog0=0.0, dist_m=50.0)
    assert (
        assess(dataclasses.replace(moored, duration_s=S.gap_stationary_s - 1.0), 1, 1, 3, S).classification
        == "position_jump"
    )
    assert assess(dataclasses.replace(moored, duration_s=S.gap_stationary_s + 1.0), 1, 1, 3, S).classification == "dark"
    # "Moored" but 6 km from where it was: it moved, so the short threshold applies.
    assert assess(gap(sog0=0.0, dist_m=6000.0, duration_s=2000.0), 1, 1, 3, S).classification == "dark"


def test_score_is_bounded_and_monotonic_in_duration():
    scores = [assess(gap(duration_s=h * 3600.0, dist_m=1000.0), 1, 1, 10, S).score for h in (1, 3, 6, 12, 48)]
    assert scores == sorted(scores) and scores[-1] == scores[-2]
    assert assess(gap(duration_s=86400.0, dist_m=5e7), 1, 1, 50, S).score == 100.0


# --- the simulator ---------------------------------------------------------------------------------------


def test_simulator_is_deterministic_and_carries_the_profiled_defects():
    def run(seconds=5400):
        w = World(seed=11, n_ships=120)
        return [line for _, line in w.sentences(1_791_500_000, 1_791_500_000 + seconds)], w.truth

    lines, _ = run()
    assert lines == run()[0]
    assert len(lines) > 5000
    truth = run(5 * 3600)[1]
    broken = sum(not identity_check(ln) for ln in lines) / len(lines)
    repeats = (len(lines) - len(set(lines))) / len(lines)
    assert 0.002 < broken < 0.02 and 0.02 < repeats < 0.06
    assert {t.cause for t in truth} >= {"dark", "range"}
    assert all(t.end - t.start >= 1800 for t in truth)


def test_clean_simulator_emits_only_valid_sentences():
    w = World(seed=11, n_ships=60, defects=False)
    lines = [line for _, line in w.sentences(1_791_500_000, 1_791_500_000 + 900)]
    assert lines and all(identity_check(ln) for ln in lines)
