-- Per-user "most" facts that need the event grain: top repository, busiest hour of day, first event.
-- Ties are broken deterministically so a re-run tells the user the same thing.

with events as (
    select * from {{ ref('int_events__deduped') }}
    where year(activity_date) = {{ var('wrapped_year') }}
),

repo_counts as (
    select user_id, repo_id, arg_max(repo_name, occurred_at) as repo_name, count(*) as events
    from events
    where repo_id is not null and repo_name is not null
    group by user_id, repo_id
),

repos as (
    select
        user_id,
        count(*) as distinct_repos,
        arg_max(repo_name, (events, -repo_id)) as top_repo_name,
        max(events) as top_repo_events
    from repo_counts
    group by user_id
),

hour_counts as (
    select user_id, hour(occurred_at) as hour_utc, count(*) as events
    from events
    group by user_id, hour(occurred_at)
),

hours as (
    select
        user_id,
        arg_max(hour_utc, (events, -hour_utc)) as peak_hour_utc,
        max(events) as peak_hour_events
    from hour_counts
    group by user_id
),

firsts as (
    select
        user_id,
        min(occurred_at) as first_event_at,
        max(occurred_at) as last_event_at,
        arg_min(activity, (occurred_at, event_id)) as first_activity,
        arg_min(repo_name, (occurred_at, event_id)) as first_repo_name
    from events
    group by user_id
)

select
    f.user_id,
    coalesce(r.distinct_repos, 0) as distinct_repos,
    r.top_repo_name,
    r.top_repo_events,
    h.peak_hour_utc,
    h.peak_hour_events,
    f.first_event_at,
    f.last_event_at,
    f.first_activity,
    f.first_repo_name
from firsts f
left join repos r using (user_id)
left join hours h using (user_id)
