-- The thresholds a user can be compared against, and exactly how many people clear each one.
--
-- How a percentile is computed without sorting the user base:
--   1. One quantile sketch (t-digest, approx_quantile) per metric, built in a single streaming pass,
--      proposes ~100 threshold values. The sketch is approximate and that is fine: it only chooses
--      WHERE the thresholds sit.
--   2. One hash aggregation counts exactly how many users fall in each bucket between thresholds.
--      A running sum over those ~100 rows gives the exact number of users at or above each threshold.
--
-- So "top_fraction" is an exact count divided by an exact population, never a sketch estimate.
-- A claim built on it cannot be wrong; sketch error only makes the thresholds a little coarser.
--
-- Privacy: a threshold that fewer than k users clear is not published. Being told you are in a
-- group of two is being told about the other person.

with metric_values as (
    select * from {{ ref('mart_metric_values') }}
),

sketches as (
    select metric, list_distinct(approx_quantile(value, {{ quantile_ladder() }})) as cuts
    from metric_values
    group by metric
),

bucketed as (
    select
        v.metric,
        list_max(list_filter(s.cuts, lambda c: c <= v.value)) as cut_value,
        count(*) as users_in_bucket
    from metric_values v
    join sketches s using (metric)
    group by all
),

cumulative as (
    select
        metric,
        cut_value,
        users_in_bucket,
        cast(sum(users_in_bucket) over (partition by metric order by cut_value desc) as bigint) as users_at_or_above
    from bucketed
    where cut_value is not null
),

populations as (
    -- Share metrics are ranked only among the users they are defined for; everything else among all people.
    select
        s.metric,
        case
            when s.metric = 'weekend_share' then (select count(weekend_share) from {{ ref('mart_user_year') }})
            else (select count(*) from {{ ref('mart_user_year') }})
        end as population
    from sketches s
)

select
    c.metric,
    c.cut_value,
    c.users_in_bucket,
    c.users_at_or_above,
    p.population,
    c.users_at_or_above / p.population as top_fraction
from cumulative c
join populations p using (metric)
where c.users_at_or_above >= {{ var('k_anonymity') }}
