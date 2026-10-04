"""Business-logic tests that need no database and no network."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest

from parse_app import metrics as M
from parse_app.active import diverse_top, select
from parse_app.embed import HashEmbedder
from parse_app.feed import inject_defects, parse_mabsa_line, synthetic_records
from parse_app.fetch import FetchError, download
from parse_app.hier import HierModel, fit, fit_calibrated
from parse_app.ingest import Accepted, Rejected, gold_paths, validate
from parse_app.taxonomy import Tree, build_paths, canonical_path, resolve

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def rec(**kw: object) -> bytes:
    base = {"id": "a1", "text": "the room was dirty", "lang": "en", "created_at": "2026-03-01T10:00:00Z"}
    return json.dumps({**base, **kw}).encode()


# --- taxonomy ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "raw", "expected"),
    [
        ("hotel", "rooms comfort", ("hotel", "rooms", "comfort")),
        ("hotel", "rooms design_features", ("hotel", "rooms", "design_features")),
        ("laptop", "BATTERY#OPERATION_PERFORMANCE", ("laptop", "battery", "operation_performance")),
        ("laptop", "FANS&COOLING#GENERAL", ("laptop", "fans_cooling", "general")),
        ("phone", "Battery/Longevity#Battery Life", ("phone", "battery_longevity", "battery_life")),
        ("sight", "Course_General_Feedback", ("sight", "course_general_feedback")),
        ("hotel", "polarity negative", None),  # sentiment leaked into the category field upstream
        ("hotel", "  ", None),
    ],
)
def test_canonical_path_unifies_upstream_spellings(domain: str, raw: str, expected: tuple[str, ...] | None) -> None:
    assert canonical_path(domain, raw) == expected


def test_build_paths_folds_rare_nodes_and_never_orphans() -> None:
    counts = Counter({("h", "rooms", "comfort"): 20, ("h", "rooms", "smell"): 2, ("h", "pool", "size"): 3})
    paths = build_paths(counts, ["h", "empty_domain"], min_support=8)
    assert paths == ["empty_domain", "h", "h/rooms", "h/rooms/comfort"]
    # folded leaves resolve to the nearest surviving ancestor
    assert resolve(("h", "rooms", "smell"), set(paths)) == "h/rooms"
    assert resolve(("h", "pool", "size"), set(paths)) == "h"
    assert resolve(("nope", "x"), set(paths)) is None


def tree7() -> Tree:
    #        1          2
    #      3   4        5
    #     6
    return Tree.from_edges([(6, 3), (3, 1), (4, 1), (1, None), (2, None), (5, 2)])


def test_tree_is_topological_and_closure_adds_ancestors() -> None:
    t = tree7()
    assert all(p < j for j, p in enumerate(t.parent))
    y = t.encode([[6], [], [5, 4]])
    labels = [{int(t.node_ids[j]) for j in np.flatnonzero(row)} for row in y]
    assert labels == [{6, 3, 1}, set(), {5, 2, 4, 1}]
    assert t.is_consistent(y).all()
    broken = y.copy()
    broken[0, t.index[1]] = False
    assert t.is_consistent(broken).tolist() == [False, True, True]


# --- hierarchical classifier -----------------------------------------------------------------------


def toy_problem(n: int = 600, seed: int = 0, noise: float = 0.8) -> tuple[np.ndarray, np.ndarray, Tree]:
    """Each node owns a random direction; an item's vector is the sum of its nodes' directions + noise."""
    rng = np.random.default_rng(seed)
    t = tree7()
    leaves = np.flatnonzero(t.is_leaf)
    y = np.zeros((n, len(t)), dtype=bool)
    y[np.arange(n), rng.choice(leaves, n)] = True
    y = t.close(y)
    directions = rng.normal(size=(len(t), 32))
    x = y @ directions + rng.normal(scale=noise, size=(n, 32))
    return x.astype(np.float32), y, t


def test_training_smoke_learns_and_beats_prior() -> None:
    x, y, t = toy_problem()
    model = fit_calibrated(x[:450], y[:450], t)
    pred = model.decode(model.marginals(x[450:]))
    learned = M.summary(y[450:], pred, t)["hf1"]
    prior = M.summary(y[450:], np.tile(y[:450].mean(0) >= 0.5, (150, 1)), t)["hf1"]
    assert learned > 0.85 and learned > prior + 0.2


@pytest.mark.parametrize("seed", range(5))
def test_consistency_holds_for_arbitrary_weights(seed: int) -> None:
    """The guarantee must not depend on training going well: random weights, random calibration,
    random inputs - marginals stay monotone and decoded sets stay ancestor-closed."""
    rng = np.random.default_rng(seed)
    t = tree7()
    model = HierModel(
        tree=t, mu=np.zeros(16), w=rng.normal(scale=3, size=(len(t), 16)), b=rng.normal(scale=3, size=len(t)),
        trained=np.ones(len(t), dtype=bool), cal_a=rng.uniform(0.2, 3, 8), cal_b=rng.normal(scale=2, size=8),
        threshold=float(rng.uniform(0.1, 0.9)),
    )  # fmt: skip
    x = rng.normal(size=(500, 16))
    p = model.marginals(x)
    child = t.parent >= 0
    assert (p[:, child] <= p[:, t.parent[child]] + 1e-12).all()
    assert t.is_consistent(model.decode(p.copy())).all()
    conf = model.confidence(p)
    assert ((conf >= 0) & (conf <= 1)).all()


def test_flat_baseline_can_violate_the_hierarchy() -> None:
    """Shows the enforcement is doing work: the same data without it produces orphan labels."""
    x, y, t = toy_problem(seed=3, noise=4.0)
    flat = fit(x[:80], y[:80], t, hierarchical=False)
    assert not t.is_consistent(flat.decode(flat.marginals(x[80:]))).all()


def test_untrained_node_keeps_small_nonzero_probability() -> None:
    """A node nobody has labelled yet must stay discoverable (p > 0) without being predicted."""
    x, y, t = toy_problem()
    j = t.index[4]  # sibling of node 3 under root 1
    keep = ~y[:, j]
    model = fit(x[keep], y[keep], t)
    assert not model.trained[j]
    p = model.marginals(x[:50])[:, j]
    assert (p > 0).all() and (p < 0.05).all()
    assert not model.decode(model.marginals(x[:50]))[:, j].any()


def test_inference_contract_and_artifact_roundtrip() -> None:
    x, y, t = toy_problem()
    model = fit_calibrated(x, y, t)
    clone = HierModel.from_bytes(model.to_bytes())
    p, q = model.marginals(x[:50]), clone.marginals(x[:50])
    assert p.shape == (50, len(t)) and p.dtype == np.float64
    assert ((p >= 0) & (p <= 1)).all()
    np.testing.assert_allclose(p, q, atol=1e-5)  # float32 storage
    assert (model.decode(p.copy()) == clone.decode(q.copy())).mean() > 0.999
    assert clone.threshold == model.threshold and (clone.tree.node_ids == t.node_ids).all()


# --- active learning -------------------------------------------------------------------------------


def test_entropy_prefers_ambiguous_items_and_selection_respects_candidates() -> None:
    x, y, t = toy_problem()
    model = fit(x[:300], y[:300], t)
    clear = x[300:400]
    ambiguous = (x[300:400] + x[400:500][::-1]) / 2  # blends of two different items
    assert model.entropy(ambiguous).mean() > model.entropy(clear).mean()

    rng = np.random.default_rng(0)
    candidates = np.arange(300, 600)
    for strategy in ("random", "entropy", "entropy_diverse"):
        picked = select(strategy, model, x, candidates, 25, rng)
        assert len(picked) == len(set(picked.tolist())) == 25
        assert set(picked.tolist()) <= set(candidates.tolist())
    assert len(select("entropy", None, x, candidates, 10, rng)) == 10  # cold start falls back to random
    with pytest.raises(ValueError, match="unknown strategy"):
        select("nope", model, x, candidates, 5, rng)


def test_diverse_top_does_not_spend_the_batch_on_near_duplicates() -> None:
    rng = np.random.default_rng(1)
    centres = rng.normal(size=(6, 8)) * 10
    x = np.repeat(centres, 20, axis=0) + rng.normal(scale=0.01, size=(120, 8))
    scores = np.zeros(120)
    scores[:20] = 5.0  # one cluster is uniformly the most uncertain
    scores[20:] = rng.uniform(1, 2, 100)
    top_plain = np.argsort(-scores)[:6]
    assert len({i // 20 for i in top_plain}) == 1
    assert len({int(i) // 20 for i in diverse_top(x, scores, 6, seed=0)}) >= 4


# --- metrics ---------------------------------------------------------------------------------------


def test_metrics_known_values() -> None:
    t = tree7()
    y_true = t.encode([[6], [4], [5]])
    y_pred = t.encode([[3], [4], [4]])  # right parent of a wrong leaf; exact; wrong root
    s = M.summary(y_true, y_pred, t, macro_min_support=1)
    # tp: (1,3) + (1,4) = 4 ; fp: item3 -> 1,4 = 2 ; fn: item1 -> 6, item3 -> 2,5 = 3
    assert s["precision"] == pytest.approx(4 / 6) and s["recall"] == pytest.approx(4 / 7)
    assert s["hf1"] == pytest.approx(8 / 13)
    assert s["exact_match"] == pytest.approx(1 / 3) and s["consistent"] == 1.0
    assert M.prf(0, 0, 0) == (0.0, 0.0, 0.0)
    lo, hi = M.wilson(8, 10)
    assert 0.49 < lo < 0.50 and 0.94 < hi < 0.95
    assert M.wilson(0, 0) == (0.0, 1.0)


def test_ece_is_zero_when_calibrated_and_large_when_overconfident() -> None:
    rng = np.random.default_rng(0)
    conf = rng.uniform(0.05, 0.95, 20000)
    assert M.ece(conf, rng.uniform(size=20000) < conf) < 0.02
    assert M.ece(np.full(1000, 0.99), rng.uniform(size=1000) < 0.6) > 0.3


def test_bootstrap_interval_brackets_the_point_estimate() -> None:
    x, y, t = toy_problem()
    model = fit(x[:300], y[:300], t)
    pred = model.decode(model.marginals(x[300:]))
    lo, hi = M.bootstrap_hf1(y[300:], pred, np.arange(300) // 3)
    assert lo <= M.summary(y[300:], pred, t)["hf1"] <= hi and hi - lo < 0.2


# --- boundary validation ---------------------------------------------------------------------------


def test_validate_accepts_and_normalises() -> None:
    r = validate(rec(text="  the  room\u0007 was\tdirty \n", lang="EN-us", created_at=1772359200), "feed", NOW)
    assert isinstance(r, Accepted)
    assert r.text == "the room was dirty" and r.lang == "en" and r.split == "pool"
    assert r.created_at == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    assert r.group_key == "id:feed:a1" and r.source == "feed" and not r.repaired


def test_validate_tolerates_missing_optional_fields() -> None:
    r = validate(json.dumps({"text": "no id, no lang, no time"}).encode(), "feed", NOW)
    assert isinstance(r, Accepted)
    assert r.lang is None and r.created_at is None and r.external_id.startswith("sha:")
    again = validate(json.dumps({"text": "no id,  no lang, no time "}).encode(), "feed", NOW)
    assert isinstance(again, Accepted) and again.external_id == r.external_id  # redelivery still dedupes
    odd = validate(rec(lang="klingon!"), "feed", NOW)
    assert isinstance(odd, Accepted) and odd.lang is None


def test_validate_repairs_mojibake_but_leaves_real_latin1_alone() -> None:
    original = "la habitación estaba sucia"
    r = validate(rec(text=original.encode("utf-8").decode("latin-1")), "feed", NOW)
    assert isinstance(r, Accepted) and r.text == original and r.repaired
    r = validate(rec(text="café déjà vu"), "feed", NOW)
    assert isinstance(r, Accepted) and r.text == "café déjà vu" and not r.repaired


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b'{"id": "a", "text": "trunc', "malformed_json"),
        (b"[1, 2]", "malformed_json"),
        (b'{"text\xff": "x"}', "bad_encoding"),
        (rec(text=None), "missing_text"),
        (json.dumps({"id": "a"}).encode(), "missing_text"),
        (rec(text=" \n\t "), "empty_text"),
        (rec(text=12), "bad_field"),
        (rec(text="x" * 4001), "too_long"),
        (rec(created_at="yesterday"), "bad_timestamp"),
        (rec(created_at="2099-01-01T00:00:00Z"), "bad_timestamp"),
        (rec(id=""), "bad_field"),
        (rec(source="Has Spaces"), "bad_field"),
        (rec(eval=True), "bad_field"),  # an eval record without gold would poison the test set
        (rec(gold={"domain": "hotel"}), "bad_field"),
        (rec(eval="yes"), "bad_field"),
    ],
)
def test_validate_rejects_with_a_named_reason(raw: bytes, reason: str) -> None:
    r = validate(raw, "feed", NOW)
    assert isinstance(r, Rejected) and r.reason == reason and r.detail


def test_gold_paths_maps_and_counts_unmappable() -> None:
    known = {"hotel": 1, "hotel/rooms": 2, "hotel/rooms/comfort": 3}
    nodes, unmapped = gold_paths(("hotel", ("rooms comfort", "rooms smell", "polarity negative")), known)
    assert nodes == {1, 2, 3} and unmapped == 1  # smell folds to rooms; polarity is not an aspect
    assert gold_paths(("hotel", ()), known) == ({1}, 0)  # no aspect: still a hotel sentence


def test_mabsa_line_parser_survives_broken_quoting() -> None:
    assert parse_mabsa_line("Great bed .####[['bed', 'rooms comfort', 'positive']]") == (
        "Great bed .",
        ["rooms comfort"],
    )
    broken = "Kozi nzuri####[['Dr. Ng's', 'faculty general', 'positive'], ['NULL', 'course general', 'POS']]"
    assert parse_mabsa_line(broken) == ("Kozi nzuri", ["course general", "faculty general"])
    assert parse_mabsa_line("No aspect here####[]") == ("No aspect here", [])
    assert parse_mabsa_line("") is None and parse_mabsa_line("no separator") is None


# --- feed generator --------------------------------------------------------------------------------


def test_synthetic_feed_is_deterministic_and_has_the_real_shape() -> None:
    a = list(inject_defects(synthetic_records(400, 50, seed=7), seed=7))
    b = list(inject_defects(synthetic_records(400, 50, seed=7), seed=7))
    assert a == b and a != list(inject_defects(synthetic_records(400, 50, seed=8), seed=8))

    results = [validate(line, "feed", NOW) for line in a]
    accepted = [r for r in results if isinstance(r, Accepted)]
    reasons = Counter(r.reason for r in results if isinstance(r, Rejected))
    assert len(reasons) >= 3 and 0 < sum(reasons.values()) < 0.05 * len(a)
    test = [r for r in accepted if r.split == "test"]
    assert len(test) == 50 * 4  # parallel: every test group in every language, untouched by defects
    by_group: dict[str, set[tuple[str, tuple[str, ...]]]] = {}
    for r in test:
        by_group.setdefault(r.group_key, set()).add(r.gold)  # type: ignore[arg-type]
    assert all(len(golds) == 1 for golds in by_group.values())
    assert not {r.group_key for r in test} & {r.group_key for r in accepted if r.split == "pool"}
    assert any(r.lang is None for r in accepted) and any(r.created_at is None for r in accepted)


def test_hash_embedder_is_deterministic_and_normalised() -> None:
    e = HashEmbedder()
    v = e.embed(["the room was dirty", "the room was  dirty", "battery dies fast"])
    assert v.shape == (3, 384) and v.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(v[:3], axis=1), 1.0, atol=1e-5)
    np.testing.assert_array_equal(v[0], v[1])  # whitespace-normalised before hashing
    assert v[0] @ v[2] < 0.5


# --- external dependency: download with backoff ----------------------------------------------------


def test_download_retries_with_capped_backoff_then_succeeds(tmp_path: Path) -> None:
    calls, sleeps = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        if len(calls) < 4:
            return httpx.Response(503) if len(calls) % 2 else (_ for _ in ()).throw(httpx.ConnectError("boom"))
        return httpx.Response(200, content=b"payload")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = download("https://example.test/f", tmp_path / "f.bin", client=client, sleep=sleeps.append, max_delay=3.0)
    assert dest.read_bytes() == b"payload" and len(calls) == 4
    assert sleeps == [1.0, 2.0, 3.0]  # exponential, capped
    download("https://example.test/f", dest, client=client, sleep=sleeps.append)
    assert len(calls) == 4  # idempotent: existing file is not fetched again


def test_download_gives_up_after_the_cap_and_leaves_no_partial_file(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(502)))
    sleeps: list[float] = []
    with pytest.raises(FetchError, match="after 3 attempts"):
        download("https://example.test/f", tmp_path / "f.bin", client=client, attempts=3, sleep=sleeps.append)
    assert len(sleeps) == 2 and list(tmp_path.iterdir()) == []


def test_download_does_not_retry_a_permanent_error(tmp_path: Path) -> None:
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(404)))
    with pytest.raises(FetchError, match="not retryable"):
        download("https://example.test/f", tmp_path / "f.bin", client=client, sleep=lambda _: None)
    assert len(calls) == 1
