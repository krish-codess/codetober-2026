-- Longest run of consecutive active UTC days per user in the year (gaps and islands).
-- A streak is clipped to the year: days in the previous December do not extend it.

with days as (
    select user_id, activity_date
    from {{ ref('int_user_days') }}
    where year(activity_date) = {{ var('wrapped_year') }}
),

islands as (
    select
        user_id,
        activity_date,
        activity_date - cast(row_number() over (partition by user_id order by activity_date) as integer) as island
    from days
),

runs as (
    select user_id, count(*) as days, min(activity_date) as started_on, max(activity_date) as ended_on
    from islands
    group by user_id, island
)

select
    user_id,
    max(days) as longest_streak_days,
    -- On a tie, the earliest streak.
    arg_max(started_on, (days, -epoch(started_on))) as streak_started_on,
    arg_max(ended_on, (days, -epoch(started_on))) as streak_ended_on
from runs
group by user_id
