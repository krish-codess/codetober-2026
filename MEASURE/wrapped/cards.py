"""The card catalogue, the superlative selector, and the words. Pure functions, no I/O.

Rules this module exists to enforce:

* A comparison ("top 5%") is only ever built from an exact count: `users_at_or_above` people out of
  `population` have a value at least as high as a threshold this user clears. The percent shown is
  the smallest rung of LADDER that the exact ratio fits under, compared in integers. A user in the
  top 5.2% is told "top 10%", never "top 5%".
* A user's own share ("41% of your events") is truncated, never rounded up.
* Every number in a card is in its `facts`, named after the warehouse column it came from, so the
  audit can check each one against the source.
* Nothing about any other individual appears anywhere. Comparisons are against population counts
  that the warehouse only publishes for groups of at least k.
* A user with one event gets a story built from what they did, not a list of what they did not.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
import zlib
from typing import Any, NamedTuple

CATALOGUE_VERSION = 1

# Percent rungs a claim may use. Stops at 25: "top 40%" is true and nobody's idea of a superlative.
LADDER_PERMILLE = [1, 5, 10, 20, 30, 50, 100, 150, 200, 250]

MONTHS = [calendar.month_name[i] for i in range(13)]
TIER_CARDS = {"full": 5, "light": 4, "minimal": 3}
FAMILY_LIMIT = {"craft": 2}  # every other family: 1
# Story pacing: where you started, how much, when, where, how steadily, how widely, what kind.
FAMILY_ORDER = [
    "origin",
    "volume",
    "rhythm",
    "place",
    "consistency",
    "breadth",
    "craft",
    "curiosity",
    "peak",
    "community",
]


class Rank(NamedTuple):
    users_at_or_above: int
    population: int


class MetricCard(NamedTuple):
    metric: str
    family: str
    singular: str
    plural: str
    headline: str
    body: str
    archetype: str
    noun: str  # how a claim names the metric: "Top 5% for <noun>"
    minimum: int = 1  # below this the card is not worth showing even though it is true


METRIC_CARDS: dict[str, MetricCard] = {
    "total_events": MetricCard("events", "volume", "event", "events", "You made things happen",
                               "{events} across {active_days}.", "The Powerhouse", "total activity"),
    "active_days": MetricCard("active_days", "consistency", "active day", "active days", "You kept showing up",
                              "You were active on {value} of the year's {year_days} days.", "The Regular", "active days", 2),
    "streak": MetricCard("longest_streak_days", "consistency", "day in a row", "days in a row", "Your longest streak",
                         "From {streak_started_on} to {streak_ended_on}, you did not miss a day.", "The Marathoner", "longest streak", 3),
    "busiest_day": MetricCard("busiest_day_events", "peak", "event in one day", "events in one day", "Your biggest day",
                              "{busiest_date} was the busiest day of your year.", "The Sprinter", "biggest single day", 5),
    "explorer": MetricCard("distinct_repos", "breadth", "repository", "repositories", "You got around",
                           "Your activity reached {value} different repositories.", "The Explorer", "repositories reached", 2),
    "pushes": MetricCard("pushes", "craft", "push", "pushes", "You shipped code",
                         "Code left your machine {value} times this year.", "The Builder", "pushes"),
    "prs_opened": MetricCard("prs_opened", "craft", "pull request opened", "pull requests opened", "You proposed changes",
                             "You opened {value} pull requests.", "The Contributor", "pull requests opened"),
    "reviews": MetricCard("reviews", "craft", "review", "reviews", "You read other people's code",
                          "Reviews and review comments you left on pull requests: {value}.", "The Reviewer", "reviews"),
    "issues_opened": MetricCard("issues_opened", "craft", "issue opened", "issues opened", "You spotted things",
                                "You opened {value} issues.", "The Scout", "issues opened"),
    "comments": MetricCard("comments", "craft", "comment", "comments", "You joined the conversation",
                           "You left {value} comments on issues and commits.", "The Conversationalist", "comments"),
    "releases": MetricCard("releases", "craft", "release", "releases", "You shipped it",
                           "You published {value} releases.", "The Shipper", "releases"),
    "stars": MetricCard("stars", "curiosity", "star given", "stars given", "You know what is good",
                        "You handed out {value} stars.", "The Stargazer", "stars given"),
    "forks": MetricCard("forks", "curiosity", "fork", "forks", "You made it your own",
                        "You forked {value} times.", "The Tinkerer", "forks"),
}  # fmt: skip

# card_type -> (family, shareable). The single source for the card_types table.
CARD_TYPES: dict[str, tuple[str, bool]] = {
    "intro": ("frame", False),
    "summary": ("frame", True),
    **{name: (card.family, True) for name, card in METRIC_CARDS.items()},
    "weekend": ("rhythm", True),
    "peak_hour": ("rhythm", True),
    "busiest_month": ("rhythm", True),
    "home_base": ("place", True),
    "first_event": ("origin", True),
    "community": ("community", False),
}

# Fixed interest of the cards that carry no comparison, by tier. Claims score in bits of surprise
# (top 10% = 3.3, top 1% = 6.6), so these numbers decide when a plain fact beats a weak comparison.
# Each gets up to +1 of per-user jitter, which is what stops quiet users all receiving the same story.
# "activity" is a claimless card about something the user did (a push, a star); "stat" is a claimless
# count (events, active days), which for a quiet user would be the sad card, so it has no score there.
PLAIN_SCORE = {
    "full": {"home_base": 1.6, "peak_hour": 1.3, "busiest_month": 1.1, "first_event": 0.4, "community": 0.2,
             "activity": 0.3, "stat": 0.3},
    "light": {"first_event": 2.0, "home_base": 1.8, "activity": 1.7, "community": 1.2, "peak_hour": 1.2,
              "busiest_month": 1.2},
    "minimal": {"first_event": 2.4, "home_base": 2.1, "activity": 2.0, "busiest_month": 1.6, "community": 1.5},
}  # fmt: skip
FIRST_ACTIVITY = {
    "push": "a push to", "pr_opened": "a pull request on", "review": "a review on", "issue_opened": "an issue on",
    "comment": "a comment on", "star": "a star for", "fork": "a fork of", "release": "a release of",
    "repo_created": "a new repository,", "other": "some activity on",
}  # fmt: skip


def tier_of(events: int, active_days: int) -> str:
    if events < 5:
        return "minimal"
    if events < 20 or active_days < 3:
        return "light"
    return "full"


def claim_permille(rank: Rank | None) -> int | None:
    """Smallest ladder rung (in tenths of a percent) the user provably fits under, or None.

    Integer arithmetic on exact counts: no float can round a user across a threshold.
    """
    if rank is None or rank.population <= 0:
        return None
    for rung in LADDER_PERMILLE:
        if rank.users_at_or_above * 1000 <= rung * rank.population:
            return rung
    return None


def _percent(permille: int) -> str:
    return f"{permille // 10}" if permille % 10 == 0 else f"{permille / 10:.1f}"


def _n(count: int, singular: str, plural: str) -> str:
    return f"{count:,} {singular if count == 1 else plural}"


def _day(value: dt.date | dt.datetime) -> str:
    return f"{value.day} {MONTHS[value.month]}"


def _jitter(user_id: int, card_type: str) -> float:
    """Deterministic per-user tie-breaker in [0, 1): equal scores do not resolve the same way for everyone."""
    return zlib.crc32(f"{user_id}:{card_type}".encode()) / 2**32


def _claim(metric: str, noun: str, rank: Rank | None, basis: str) -> dict[str, Any] | None:
    permille = claim_permille(rank)
    if permille is None or rank is None:
        return None
    return {
        "metric": metric,
        "top_permille": permille,
        "text": f"Top {_percent(permille)}% for {noun}",
        "basis": basis.format(population=f"{rank.population:,}"),
    }


def _candidates(
    row: dict[str, Any], ranks: dict[str, Rank], population: dict[str, Any], tier: str
) -> list[dict[str, Any]]:
    """Every card that would be true for this user, each with a score."""
    year = int(population["year"])
    user_id = int(row["user_id"])
    events = int(row["events"])
    out: list[dict[str, Any]] = []

    def add(card_type: str, score: float, card: dict[str, Any]) -> None:
        family, shareable = CARD_TYPES[card_type]
        out.append({"type": card_type, "family": family, "shareable": shareable, "score": score, **card})

    for card_type, spec in METRIC_CARDS.items():
        value = int(row[spec.metric] or 0)
        if value < spec.minimum:
            continue
        rank = ranks.get(spec.metric)
        claim = _claim(spec.metric, spec.noun, rank, "among {population} people active this year")
        # Bits of surprise for a comparison. Without one, a tier-dependent constant (or not offered at all).
        if claim and rank:
            metric_score = math.log2(rank.population / rank.users_at_or_above) + 0.2 * _jitter(user_id, card_type)
        else:
            base = PLAIN_SCORE[tier].get("activity" if spec.family in ("craft", "curiosity") else "stat")
            if base is None:
                continue
            metric_score = base + _jitter(user_id, card_type)
        facts: dict[str, Any] = {spec.metric: value}
        fmt: dict[str, str] = {"value": f"{value:,}"}
        if card_type == "total_events":
            facts["active_days"] = int(row["active_days"])
            fmt |= {
                "events": _n(value, "event", "events"),
                "active_days": _n(facts["active_days"], "active day", "active days"),
            }
        elif card_type == "active_days":
            fmt["year_days"] = str(366 if calendar.isleap(year) else 365)
        elif card_type == "streak":
            facts |= {"streak_started_on": row["streak_started_on"], "streak_ended_on": row["streak_ended_on"]}
            fmt |= {
                "streak_started_on": _day(row["streak_started_on"]),
                "streak_ended_on": _day(row["streak_ended_on"]),
            }
        elif card_type == "busiest_day":
            facts["busiest_date"] = row["busiest_date"]
            fmt["busiest_date"] = _day(row["busiest_date"])
        body = spec.body.format(**fmt)
        if value == 1:  # the templates are written for the plural
            body = {"pushes": "Code left your machine once this year.", "prs_opened": "You opened a pull request.",
                    "reviews": "You left a review on a pull request.", "issues_opened": "You opened an issue.",
                    "comments": "You left a comment.", "releases": "You published a release.",
                    "stars": "You handed out a star.", "forks": "You forked a repository."}.get(card_type, body)  # fmt: skip
        add(card_type, metric_score, {
            "headline": spec.headline, "value": f"{value:,}", "unit": spec.singular if value == 1 else spec.plural,
            "body": body, "claim": claim, "facts": facts,
        })  # fmt: skip

    weekend_rank = ranks.get("weekend_share")
    if row.get("weekend_share") is not None and weekend_rank:
        claim = _claim(
            "weekend_share", "weekend activity", weekend_rank, "among the {population} people active enough to compare"
        )
        if claim:  # without a comparison, a weekend share is not a superlative
            weekend_events = int(row["weekend_events"])
            pct = weekend_events * 100 // events  # truncated
            add("weekend", math.log2(weekend_rank.population / weekend_rank.users_at_or_above) + 0.2 * _jitter(user_id, "weekend"), {
                "headline": "Weekends were yours", "value": f"{pct}%", "unit": "of your activity on weekends",
                "body": f"{_n(weekend_events, 'event', 'events')} of your {events:,} landed on a Saturday or Sunday (UTC).",
                "claim": claim, "facts": {"weekend_events": weekend_events, "events": events},
            })  # fmt: skip

    plain = PLAIN_SCORE[tier]

    def plain_score(card_type: str) -> float | None:
        return plain[card_type] + _jitter(user_id, card_type) if card_type in plain else None

    if (score := plain_score("home_base")) is not None and row.get("top_repo_name"):
        top = int(row["top_repo_events"])
        if top == events:
            body = {1: "Your one event of the year happened here.", 2: "Both of your events happened here."}.get(
                events, f"All {events:,} of your events happened here."
            )
        else:
            body = f"{top:,} of your {events:,} events happened here. That is {top * 100 // events}%."
        add("home_base", score, {
            "headline": "Your home base", "value": row["top_repo_name"], "unit": "", "body": body, "claim": None,
            "facts": {"top_repo_name": row["top_repo_name"], "top_repo_events": top, "events": events},
        })  # fmt: skip

    if (score := plain_score("peak_hour")) is not None and int(row["peak_hour_events"] or 0) >= 3:
        hour = int(row["peak_hour_utc"])
        add("peak_hour", score, {
            "headline": "Your hour", "value": f"{hour:02d}:00", "unit": "UTC",
            "body": f"More of your events happened between {hour:02d}:00 and {(hour + 1) % 24:02d}:00 UTC than in any other "
                    f"hour: {int(row['peak_hour_events']):,} of them.",
            "claim": None, "facts": {"peak_hour_utc": hour, "peak_hour_events": int(row["peak_hour_events"])},
        })  # fmt: skip

    if (score := plain_score("busiest_month")) is not None and events >= 2:
        month = int(row["busiest_month"])
        in_month = int(row["busiest_month_events"])
        add("busiest_month", score, {
            "headline": f"{MONTHS[month]} was your month", "value": f"{in_month:,}", "unit": f"events in {MONTHS[month]}",
            "body": f"All of your {year} happened in {MONTHS[month]}." if in_month == events else f"Your busiest month of {year}.",
            "claim": None, "facts": {"busiest_month": month, "busiest_month_events": in_month, "events": events},
        })  # fmt: skip

    if (score := plain_score("first_event")) is not None:
        first: dt.datetime = row["first_event_at"]
        what = FIRST_ACTIVITY.get(str(row["first_activity"]), "activity on")
        started = (
            f"{what} {row['first_repo_name']}" if row.get("first_repo_name") else what.rsplit(" ", 1)[0].rstrip(",")
        )
        add("first_event", score, {
            "headline": "Where your year began", "value": _day(first), "unit": "",
            "body": f"Your {year} started with {started}.", "claim": None,
            "facts": {"first_event_at": first, "first_activity": row["first_activity"], "first_repo_name": row.get("first_repo_name")},
        })  # fmt: skip

    if (score := plain_score("community")) is not None:
        add("community", score, {
            "headline": "You were part of something", "value": f"{int(population['users']):,}", "unit": "people",
            "body": f"Together, {int(population['users']):,} people made {int(population['events']):,} things happen in "
                    f"{year}. Your part is in there.",
            "claim": None, "facts": {"population_users": int(population["users"]), "population_events": int(population["events"])},
        })  # fmt: skip
    return out


def select(candidates: list[dict[str, Any]], tier: str) -> list[dict[str, Any]]:
    """Pick the most interesting true cards, at most one per family (two for craft), then order them as a story."""
    chosen: list[dict[str, Any]] = []
    per_family: dict[str, int] = {}
    for card in sorted(candidates, key=lambda c: (-c["score"], c["type"])):
        if len(chosen) == TIER_CARDS[tier]:
            break
        if per_family.get(card["family"], 0) < FAMILY_LIMIT.get(card["family"], 1):
            chosen.append(card)
            per_family[card["family"]] = per_family.get(card["family"], 0) + 1
    if not chosen:
        return chosen
    best = chosen[0]  # the reveal goes last
    rest = sorted(chosen[1:], key=lambda c: (FAMILY_ORDER.index(c["family"]), -c["score"]))
    return [*rest, best]


def build_payload(row: dict[str, Any], ranks: dict[str, Rank], population: dict[str, Any]) -> dict[str, Any]:
    """One user's whole story. Deterministic: same inputs, same bytes."""
    year = int(population["year"])
    events = int(row["events"])
    active_days = int(row["active_days"])
    tier = tier_of(events, active_days)
    story = select(_candidates(row, ranks, population, tier), tier)
    claimed = [c for c in story if c["claim"]]
    best = max(claimed, key=lambda c: c["score"]) if claimed else None
    archetype = (
        "The Weekender" if best and best["type"] == "weekend"
        else METRIC_CARDS[best["type"]].archetype if best
        else "The Minimalist" if tier != "full"
        else "The Steady Hand"
    )  # fmt: skip

    intro_body = {
        "full": f"{_n(events, 'event', 'events')}. {_n(active_days, 'active day', 'active days')}. Here is what stood out.",
        "light": "Not a loud year, but a real one. Here is what you did with it.",
        "minimal": "A small footprint is still a footprint. Here is yours.",
    }[tier]
    intro = {
        "type": "intro", "family": "frame", "shareable": False, "headline": f"Your {year}", "value": row["login"], "unit": "",
        "body": intro_body, "claim": None, "facts": {"events": events, "active_days": active_days} if tier == "full" else {},
    }  # fmt: skip
    stats = [{"label": "event" if events == 1 else "events", "value": f"{events:,}"},
             {"label": "active day" if active_days == 1 else "active days", "value": f"{active_days:,}"}]  # fmt: skip
    if best and best["type"] not in ("total_events", "active_days"):
        stats.append({"label": best["unit"], "value": best["value"]})
    summary = {
        "type": "summary", "family": "frame", "shareable": True, "headline": archetype, "value": row["login"], "unit": str(year),
        "body": f"{best['claim']['text']}, {best['claim']['basis']}." if best else f"That was your {year}.",
        "claim": best["claim"] if best else None, "stats": stats,
        "facts": {"events": events, "active_days": active_days} | (best["facts"] if best else {}),
    }  # fmt: skip

    cards = [intro, *story, summary]
    for card in cards:
        card.pop("score", None)
    return {
        "version": CATALOGUE_VERSION,
        "year": year,
        "user": {"id": int(row["user_id"]), "login": row["login"]},
        "tier": tier,
        "archetype": archetype,
        "population": {
            "users": int(population["users"]),
            "basis": "people active this year, automated accounts excluded",
        },
        "cards": cards,
    }
