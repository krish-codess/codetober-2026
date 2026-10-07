-- One row per account seen in the Wrapped year, with the decision on whether it is a person.
-- Automation is excluded from the population: in the real feed 20-30% of events come from bots,
-- and a percentile against a population that includes github-actions means nothing.

with days as (
    select * from {{ ref('int_user_days') }}
    where year(activity_date) = {{ var('wrapped_year') }}
),

logins as (
    -- Logins change; the id is the identity. Show the most recent one.
    select user_id, arg_max(login, occurred_at) as login
    from {{ ref('int_events__deduped') }}
    where year(activity_date) = {{ var('wrapped_year') }} and login is not null
    group by user_id
),

totals as (
    select user_id, sum(events) as events, max(events) as max_daily_events
    from days
    group by user_id
)

select
    t.user_id,
    coalesce(l.login, 'user-' || t.user_id::varchar) as login,
    t.events,
    t.max_daily_events,
    coalesce(l.login like '%[bot]', false) as is_labelled_bot,
    coalesce(l.login like '%[bot]', false)
        or t.max_daily_events > {{ var('automation_daily_events') }} as is_automated
from totals t
left join logins l using (user_id)
