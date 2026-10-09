-- The truth test. Recompute every user's standing the expensive, exact way (a full sort per metric,
-- which is fine in a test and is exactly what the pipeline avoids) and fail on any rank row whose
-- claim would be false: more people at or above the user than the row says, a fraction that is not
-- the stated count over the stated population, or a group smaller than k.

with exact as (
    select
        metric,
        user_id,
        -- RANGE frame: counts everyone with a value >= this user's, ties included.
        count(*) over (partition by metric order by value desc) as users_at_or_above_me
    from {{ ref('mart_metric_values') }}
)

select r.*, e.users_at_or_above_me
from {{ ref('mart_user_ranks') }} r
join exact e using (metric, user_id)
where e.users_at_or_above_me > r.users_at_or_above
   or r.top_fraction <> r.users_at_or_above / r.population
   or r.top_fraction <= 0
   or r.top_fraction > 1
   or r.users_at_or_above < {{ var('k_anonymity') }}
