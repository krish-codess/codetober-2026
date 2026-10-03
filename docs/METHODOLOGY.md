# Index methodology (method version `gs-1.0`)

This document is the specification the code implements. Every rule below has a test that fails if
the code drifts from it (named in brackets).

## 1. What is measured

A **consumer price index** for a game economy: the cost, over time, of a fixed basket of goods that
players actually buy, relative to a reference period. One index is published for every combination of

* **scope**: one server (EVE region / simulated shard server) or *all servers* (cross-server), and
* **division**: one of 7 CPI divisions (minerals, fuel & ice, moon & planetary materials, ships,
  modules & drones, ammunition & consumables, account & training services) or *all* (headline).

That is 48 series per world for EVE (5 servers + all, 7 divisions + all) and 40 for the 4-server
simulated shard.

## 2. Prices: one robust price per (server, item, day)

### 2a. Auction snapshots (simulated shard; EVE live order books)

Per snapshot, the sell asks of one item on one server are sorted and the order statistic of rank
`k = max(1, floor((n-1)/4))` is taken: an *interior lower quartile*.

* `n < 3` asks: **no price** (status `thin`). Never fabricated.
* Each listing is one vote; quantity is ignored, so one actor's volume buys no extra influence.
* **Bounded influence** [`test_one_added_listing_cannot_move_estimate_past_neighbouring_order_statistics`]:
  adding one listing at *any* price moves the estimate at most to a neighbouring order statistic
  of the original book. Re-pricing one listing moves it at most two.
* Because `k >= 1`, the cheapest listing is never the estimate: one bait listing at 0.01 ISK cannot
  set the price [`test_a_single_bait_listing_is_never_the_estimate`].
* Why the lower quartile and not the median: manipulation is asymmetric. Absurd asks above the
  market cost nothing to leave listed and accumulate (the real Jita book carries a 300,100,000 ISK
  Tritanium ask, 7·10⁷× the median; see DATA_PROFILE.md), while asks below the market are bought
  within minutes. The estimate only breaks when more than ~75% of the book is high-side trolls
  [`test_estimate_survives_high_side_trolls_up_to_three_quarters_of_the_book`].

Per day, the published price is the median of that day's snapshot estimates.

### 2b. Daily trade history (real EVE world)

ESI publishes, per region, item and day, the volume-weighted average trade price, high, low,
volume and order count. The raw daily price is the average. Days without trades are absent from
ESI and stay absent here (status `missing`).

### 2c. Day-level acceptance (both worlds)

A raw daily price is accepted or rejected by `robust_daily`:

1. **Cross-server consensus among prices that traded.** If at least 3 servers *traded* the item
   that day, a price more than 4× away from the median of those traded prices is rejected. A price
   within 4× is accepted even if its own history disagrees, which prevents lock-out after a genuine
   regime change. Only corroborated prices (rule 3) vote, so two cornered books can't outvote one
   honest market [`test_cornered_books_cannot_outvote_the_one_market_that_traded`]. The real
   0.01 ISK trade days in Heimatar are rejected here
   [`test_cross_server_consensus_rejects_the_real_001_isk_trade_day`].
   * The 4× limit is empirical. Across real EVE regions the median deviation from consensus is 4.4%
     and the 99th percentile is 2.2×. Only 0.41% of rows lie beyond 4×, and those are the 0.01 ISK
     contamination.
2. **Otherwise, causal Hampel.** The price is compared with the median of the cell's last 14
   *accepted* prices. It is rejected if the log-distance exceeds `max(5 × scale, ln 4)`, where
   `scale = 1.4826 × median |daily log change|` over the same window.
   * Using only *accepted* prices means a captured stretch cannot drag its own reference along
     [`test_a_captured_book_cannot_lock_out_honest_prices_later`].
   * The 4× floor lets real patch shocks through: a −62% overnight crash survives
     [`test_a_genuine_patch_crash_survives_both_checks`].
   * The check is **causal**: it only reads the past, so adding future days never changes a past
     decision [`test_acceptance_is_causal_future_days_never_change_past_decisions`].

3. **Trade corroboration.** A price is *corroborated* when the day had trades and their
   volume-weighted price is within 1.5× of it. For snapshots that's the inferred fills; for history
   it's the trade average itself, so history prices are always corroborated. An uncorroborated price
   that is also more than 1.5× from its reference (consensus, otherwise the cell's own accepted
   history) is rejected as `untraded_outlier`: an ask nobody pays is not a price.
   * This removes a cornered market, where one actor buys every ask and relists at 3×, inside the
     4× consensus band. It works even when the corner ends mid-day and the day's trades happen at
     the honest price [`test_an_untraded_corner_is_rejected_but_a_traded_premium_is_kept`,
     `test_a_corner_ending_mid_day_is_not_corroborated_by_the_honest_trades`].

A rejected price is not published (status `rejected`) and is listed as a `rejected_price`
manipulation event, together with the rule that fired.

## 3. Basket and weights (re-weighted quarterly, frozen)

The basket for calendar quarter **P** is built once, as soon as the reference period is complete
plus a 3-day grace period for late data. After that it is **frozen**: rows are append-only, with a
database trigger enforcing it.

| Rule | Definition |
|---|---|
| Reference period | the previous calendar quarter, clipped to available data, at least 21 days |
| Eligibility | robust price on ≥ 50% of reference days, a price in the link window, traded volume > 0 |
| Expenditure | Σ over reference days of price × traded volume. Volume is inferred fills for snapshots, ESI volume for history |
| Weight | expenditure share within the server, capped at 20% per item with excess redistributed pro rata, then × the server's share of world expenditure |
| Base price p₀ | median daily price over the **link window**: the last 7 days of the reference period |
| Fixed quantity | q = w × 1,000,000,000 / p₀, so the basket cost 1B ISK at base prices (about a month of play) |

The cap means no single item can dominate a server's index. Before capping, skill injectors
would carry most of the EVE weight. [`test_weight_cap_properties`,
`test_basket_eligibility_weights_and_base_price`, `test_basket_weights_sum_to_one_and_respect_the_cap`]

## 4. The index

For series S (a server scope and a division filter) on day t inside period P:

```
            Σ_g W_g · R_g,t                          Σ_{i∈g, observed} w_i · p_i,t / p0_i
I_t = L_P · ───────────────   with   R_g,t = ─────────────────────────────────────────
            Σ_g W_g                                     Σ_{i∈g, observed} w_i
          (g observed at t)
```

Groups `g = (server, division)` within S; `W_g` is the group's full basket weight.

* **Missing prices are imputed from their own group's observed items.** This is the standard CPI
  cell-relative method. The missing item is *not* given a price.
  * A group imputes for itself only if **at least half of its weight was observed**. Otherwise the
    whole group drops out, and the series' other groups carry its weight. A 15% remnant can't speak
    for a group whose heavy item is missing [`test_a_small_remnant_does_not_speak_for_its_group`].
  * `coverage = observed weight / total weight`.
* `coverage ≥ 0.9` → `ok`; `0.5 ≤ coverage < 0.9` → `partial`, published and flagged;
  `coverage < 0.5` → `insufficient`, **no value** (a gap in the chart, never interpolated).
  [`test_missing_item_is_imputed_from_its_own_group_not_given_a_price`,
  `test_low_coverage_publishes_no_value`]
* **Chain linking:** `L_P = L_{P−1} × J`, where `J` is the previous basket's aggregate relative
  evaluated at the link-window prices. The first period of a world has `L = 100`. At the link point
  the old and new baskets give the same level [`test_chain_link_is_continuous_at_the_link_point`].
* Differential test: the implementation equals a deliberately naive loop implementation of the
  formula above on random baskets with random gaps
  [`test_index_matches_naive_reference_implementation`]. The simulated index also tracks the same
  basket evaluated on the simulator's *true* fair prices: mean error about 1.2%, worst day under 5%
  [`test_synthetic_index_tracks_the_true_fair_price_index`].

## 5. Reproducibility: a published value never silently changes

* Raw inputs are content-addressed and write-once. Everything downstream can be rebuilt from them.
  [`test_index_is_reproducible_from_raw_into_a_fresh_database`: a second database rebuilt from raw
  matches bit for bit.]
* `item_price_daily` and `index_value` are **append-only**. A database trigger rejects UPDATE and
  DELETE even for the table owner [`test_published_tables_are_append_only_even_for_the_owner`].
  Re-publishing writes a new **vintage** only where the value changed, with a reason:
  `late_data`, `source_revision` or `method_change`. Unchanged inputs write nothing
  [`test_rerun_with_unchanged_inputs_writes_nothing`].
* `GET /v1/index?as_of=…` reproduces exactly what had been published at any past instant, and
  `GET /v1/index/revisions` shows every vintage of a value
  [`test_late_data_creates_visible_revisions_and_reprocesses_only_affected_days`].
* Baskets and link factors are frozen at creation. Late data can revise daily prices and index
  values, as new vintages, but never weights. This is the same policy national CPIs use.

## 6. Purchasing power in labour-hours

An **activity** (for example, null-sec ratting) has a nominal pay per hour, effective-dated so that
a bounty patch is a new rate, plus a list of goods produced per hour. For a server and day t:

```
wage_t        = isk_per_hour(t) + Σ yield_i × p_i,t      (null if any yield good has no price that day)
units/hour    = wage_t / p_item,t
hours/unit    = p_item,t / wage_t
basket cost_t = 1,000,000,000 × I_t / 100                (the basket that cost 1B ISK at reference)
hours/basket  = basket cost_t / wage_t
real wage_t   = wage_t × 100 / I_t
```

A mining wage is paid in goods, so it tracks prices. A ratting wage is mostly nominal ISK, so
inflation erodes it, and a deflationary patch raises it. That difference is the point of measuring
in labour-hours.

## 7. Shocks and attribution

* **Shock:** the daily log change of a series has robust z ≥ 6 against the previous 60 days
  (median, MAD floored) and moves ≥ 2%. Runs of same-direction shock days merge into one event.
* **Persistence:** the share of the move still present 3 days later. Above 0.5 is a level shift;
  near 0 is a spike that reverted. It is null until those days exist.
* **Attribution:** every patch released in the 3 days before the end of the shock day is a
  candidate, scored `relevance = topic × exp(−lag_hours / 48)`.
  * `topic = 1 / rank` of the series' division among the patch's keyword tags.
  * Otherwise `topic` is 0.8 for headline series, 0.5 for an untagged major release, and 0.15
    for anything else.
  * **No candidate means unattributed.** The system reports that it doesn't know rather than
    blaming the nearest patch.
* **Patch impact:** the log change of the 7-day median level after release versus before, with a
  robust z against the same statistic on every other day.

## 8. Inflation

`rate_w(t) = I_t / I_{t−w} − 1` for w ∈ {7, 30, 90, 365} calendar days, using published values
only; `annualised = (1 + rate)^(365/w) − 1`.
