-- Where each user stands on each metric: the highest published threshold they clear, and the exact
-- share of the population at or above it. An O(thresholds) lookup per row; no sort, no self-join.
--
-- Reading a row: "at least `users_at_or_above` of `population` people have a value >= this user's
-- threshold, and this user is one of them", i.e. the user is in the top `top_fraction`.
-- A user below the lowest published threshold has no row for that metric.

with published as (
    select metric, list(cut_value) as cuts
    from {{ ref('mart_metric_cutpoints') }}
    group by metric
),

placed as (
    select
        v.user_id,
        v.metric,
        v.value,
        list_max(list_filter(p.cuts, lambda c: c <= v.value)) as cut_value
    from {{ ref('mart_metric_values') }} v
    join published p using (metric)
)

select
    placed.user_id,
    placed.metric,
    placed.value,
    c.cut_value,
    c.users_at_or_above,
    c.population,
    c.top_fraction
from placed
join {{ ref('mart_metric_cutpoints') }} c using (metric, cut_value)
