"""The selector and the words: the rules that keep a story true, varied and kind."""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from wrapped import auth, cards
from wrapped.build import dumps
from wrapped.cards import Rank, build_payload, claim_permille, tier_of
from wrapped.render import HEIGHT, WIDTH, render_card

POPULATION = {"year": 2025, "users": 10_000, "events": 400_000}


def user(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "user_id": 4242, "login": "amber-otter-1", "events": 300, "active_days": 80, "longest_streak_days": 12,
        "streak_started_on": dt.date(2025, 3, 1), "streak_ended_on": dt.date(2025, 3, 12), "pushes": 150, "prs_opened": 20,
        "reviews": 40, "issues_opened": 5, "comments": 30, "stars": 10, "forks": 2, "releases": 1, "repos_created": 1,
        "distinct_repos": 9, "top_repo_name": "amber-otter-1/api", "top_repo_events": 123, "peak_hour_utc": 14,
        "peak_hour_events": 41, "busiest_date": dt.date(2025, 3, 9), "busiest_day_events": 22, "busiest_month": 3,
        "busiest_month_events": 90, "weekend_events": 125, "weekend_share": 125 / 300,
        "first_event_at": dt.datetime(2025, 1, 3, 9, 30), "last_event_at": dt.datetime(2025, 12, 20, 9, 30),
        "first_activity": "push", "first_repo_name": "amber-otter-1/api",
    }  # fmt: skip
    return row | over


def quiet_user(**over: object) -> dict[str, object]:
    zeros = dict.fromkeys(
        ["prs_opened", "reviews", "issues_opened", "comments", "forks", "releases", "repos_created", "pushes"], 0
    )
    return user(
        events=1, active_days=1, longest_streak_days=1, streak_started_on=dt.date(2025, 6, 3),
        streak_ended_on=dt.date(2025, 6, 3), stars=1, distinct_repos=1, top_repo_name="org-1/tools-7", top_repo_events=1,
        peak_hour_events=1, busiest_date=dt.date(2025, 6, 3), busiest_day_events=1, busiest_month=6, busiest_month_events=1,
        weekend_events=0, weekend_share=None, first_event_at=dt.datetime(2025, 6, 3, 8, 0), first_activity="star",
        first_repo_name="org-1/tools-7", **zeros,
    ) | over  # fmt: skip


@pytest.mark.parametrize(
    ("at_or_above", "population", "expected"),
    [
        (500, 10_000, 50),  # exactly 5.0% -> "top 5%"
        (501, 10_000, 100),  # 5.01% is not top 5%; the next true rung is 10%
        (520, 10_000, 100),  # the brief's case: a 5.2% user must not be told "top 5%"
        (10, 10_000, 1),  # 0.1%
        (11, 10_000, 5),
        (2_500, 10_000, 250),
        (2_501, 10_000, None),  # past the last rung: no claim at all
        (1, 3, None),  # 33%
        (0, 0, None),
    ],
)
def test_claim_never_rounds_in_the_users_favour(at_or_above: int, population: int, expected: int | None) -> None:
    assert claim_permille(Rank(at_or_above, population)) == expected


def test_claim_holds_for_every_count_in_a_population() -> None:
    population = 1_237  # awkward on purpose: nothing divides evenly
    for at_or_above in range(1, population + 1):
        rung = claim_permille(Rank(at_or_above, population))
        if rung is not None:
            assert at_or_above / population <= rung / 1000
            smaller = [r for r in cards.LADDER_PERMILLE if r < rung]
            assert all(at_or_above / population > r / 1000 for r in smaller), "a tighter true claim was available"


def test_tiers() -> None:
    assert [tier_of(1, 1), tier_of(4, 4), tier_of(5, 5), tier_of(19, 9), tier_of(20, 2), tier_of(20, 3)] == [
        "minimal", "minimal", "light", "light", "light", "full",
    ]  # fmt: skip


def test_story_shape_and_family_limits() -> None:
    ranks = {
        m: Rank(100, 10_000)
        for m in ("events", "active_days", "longest_streak_days", "pushes", "reviews", "prs_opened", "comments")
    }
    payload = build_payload(user(), ranks, POPULATION)
    types = [c["type"] for c in payload["cards"]]
    assert types[0] == "intro" and types[-1] == "summary" and len(types) == cards.TIER_CARDS["full"] + 2
    assert len(set(types)) == len(types)
    families = [c["family"] for c in payload["cards"][1:-1]]
    assert all(families.count(f) <= cards.FAMILY_LIMIT.get(f, 1) for f in families)
    assert all(
        set(c) >= {"type", "family", "shareable", "headline", "value", "unit", "body", "claim", "facts"}
        for c in payload["cards"]
    )


def test_the_rarest_true_thing_wins_and_names_the_archetype() -> None:
    ranks = {"events": Rank(2_000, 10_000), "reviews": Rank(12, 10_000), "pushes": Rank(900, 10_000)}
    payload = build_payload(user(), ranks, POPULATION)
    assert payload["archetype"] == "The Reviewer"
    assert payload["cards"][-2]["type"] == "reviews", "the strongest card is the reveal, last before the summary"
    assert payload["cards"][-2]["claim"]["text"] == "Top 0.5% for reviews"
    assert payload["cards"][-1]["claim"]["metric"] == "reviews"


def test_different_strengths_give_different_stories() -> None:
    a = build_payload(user(user_id=1), {"reviews": Rank(10, 10_000), "events": Rank(900, 10_000)}, POPULATION)
    b = build_payload(user(user_id=2), {"stars": Rank(10, 10_000), "longest_streak_days": Rank(50, 10_000)}, POPULATION)
    assert {c["type"] for c in a["cards"]} != {c["type"] for c in b["cards"]}
    assert a["archetype"] != b["archetype"]


def test_quiet_user_gets_a_story_not_a_shortfall() -> None:
    payload = build_payload(quiet_user(), {}, POPULATION)
    story = payload["cards"][1:-1]
    assert payload["tier"] == "minimal" and len(story) == cards.TIER_CARDS["minimal"]
    types = {c["type"] for c in story}
    # No bare counts of how little happened, no comparisons the user would lose.
    assert not types & {"total_events", "active_days", "streak", "busiest_day", "explorer", "peak_hour"}
    assert all(c["claim"] is None for c in payload["cards"])
    text = dumps(payload).lower()
    assert not re.search(r"\b(only|just|bottom|below|less than|fewer|never|0 )", text), text
    assert all(c["headline"] and c["body"] for c in payload["cards"])


def test_quiet_user_still_gets_a_true_rare_fact() -> None:
    # One release in a population where 3% published any: a real superlative, even on one event.
    payload = build_payload(
        quiet_user(stars=0, releases=1, first_activity="release"), {"releases": Rank(300, 10_000)}, POPULATION
    )
    release = next(c for c in payload["cards"] if c["type"] == "releases")
    assert release["claim"]["text"] == "Top 3% for releases" and release["body"] == "You published a release."
    assert payload["archetype"] == "The Shipper"


def test_quiet_users_do_not_all_get_the_same_cards() -> None:
    stories = {
        tuple(
            sorted(
                c["type"]
                for c in build_payload(
                    quiet_user(user_id=uid, events=3, top_repo_events=3, busiest_month_events=3, stars=3),
                    {},
                    POPULATION,
                )["cards"]
            )
        )
        for uid in range(1, 200)
    }
    assert len(stories) >= 4


def test_own_shares_are_truncated_never_rounded_up() -> None:
    ranks = {"weekend_share": Rank(10, 5_000)}
    payload = build_payload(user(events=300, weekend_events=125), ranks, POPULATION)  # 41.67%
    weekend = next(c for c in payload["cards"] if c["type"] == "weekend")
    assert weekend["value"] == "41%"
    candidates = cards._candidates(user(events=3, top_repo_events=2, active_days=1), {}, POPULATION, "minimal")
    assert "66%" in next(c for c in candidates if c["type"] == "home_base")["body"]  # 66.67%


def test_deterministic_and_json_safe() -> None:
    ranks = {"events": Rank(40, 10_000), "longest_streak_days": Rank(70, 10_000)}
    first, second = (
        dumps(build_payload(user(), ranks, POPULATION)),
        dumps(build_payload(user(), dict(reversed(ranks.items())), POPULATION)),
    )
    assert first == second
    assert json.loads(first)["cards"][0]["value"] == "amber-otter-1"


def test_no_threshold_value_or_group_size_reaches_the_payload() -> None:
    # The only numbers about other people are the population size and a ladder rung.
    payload = build_payload(user(), {"events": Rank(37, 9_871)}, POPULATION | {"users": 9_871})
    claim = next(c["claim"] for c in payload["cards"] if c["type"] == "total_events")
    assert set(claim) == {"metric", "top_permille", "text", "basis"}
    assert "37" not in json.dumps(claim)


def test_catalogue_is_consistent() -> None:
    assert set(cards.METRIC_CARDS) <= set(cards.CARD_TYPES)
    assert all(
        family in cards.FAMILY_ORDER
        for name, (family, _) in cards.CARD_TYPES.items()
        if name not in ("intro", "summary")
    )
    assert cards.LADDER_PERMILLE == sorted(cards.LADDER_PERMILLE)


def test_tokens() -> None:
    secret = "s" * 32
    token = auth.mint(secret, 4242, 2025, ttl_seconds=60, now=1000)
    assert auth.verify(secret, token, now=1030) == auth.Claims(4242, 2025, 1060)
    assert auth.verify(secret, token, now=1061) is None, "expired"
    assert auth.verify("other-secret-0123456789", token, now=1030) is None, "wrong key"
    body, sig = token.split(".")
    forged = auth.mint(secret, 4243, 2025, ttl_seconds=60, now=1000).split(".")[0] + "." + sig
    assert auth.verify(secret, forged, now=1030) is None, "signature does not cover another user"
    for junk in ("", ".", "abc", "abc.def", body, f"{body}.", "é.é", "a" * 600):
        assert auth.verify(secret, junk, now=1030) is None


def test_share_card_is_a_feed_sized_png_and_deterministic() -> None:
    payload = build_payload(
        user(top_repo_name="an-extremely-long-organisation-name/and-an-equally-long-repository-name"),
        {"events": Rank(10, 10_000)},
        POPULATION,
    )
    for card in payload["cards"]:
        png = render_card(card, payload["user"]["login"], 2025)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert int.from_bytes(png[16:20]) == WIDTH and int.from_bytes(png[20:24]) == HEIGHT
        assert png == render_card(card, payload["user"]["login"], 2025)
    assert render_card({}, "x", 2025)[:4] == b"\x89PNG", "a card with nothing in it still renders"
