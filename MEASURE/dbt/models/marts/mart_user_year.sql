-- One row per person for the Wrapped year: everything a card can say about them.
-- Hours and dates are UTC. We do not know anyone's timezone, so nothing here claims "night" or "morning".

with users as (
    select * from {{ ref('int_users') }}
    where not is_automated
),

days as (
    select * from {{ ref('int_user_days') }}
    where year(activity_date) = {{ var('wrapped_year') }}
),

totals as (
    select
        user_id,
        cast(sum(events) as bigint) as events,
        count(*) as active_days,
        cast(sum(pushes) as bigint) as pushes,
        cast(sum(prs_opened) as bigint) as prs_opened,
        cast(sum(reviews) as bigint) as reviews,
        cast(sum(issues_opened) as bigint) as issues_opened,
        cast(sum(comments) as bigint) as comments,
        cast(sum(stars) as bigint) as stars,
        cast(sum(forks) as bigint) as forks,
        cast(sum(releases) as bigint) as releases,
        cast(sum(repos_created) as bigint) as repos_created,
        cast(coalesce(sum(events) filter (where isodow(activity_date) >= 6), 0) as bigint) as weekend_events,
        -- On a tie, the earliest day.
        arg_max(activity_date, (events, -epoch(activity_date))) as busiest_date,
        max(events) as busiest_day_events
    from days
    group by user_id
),

months as (
    select user_id, month(activity_date) as month_number, sum(events) as events
    from days
    group by user_id, month(activity_date)
),

top_month as (
    select
        user_id,
        arg_max(month_number, (events, -month_number)) as busiest_month,
        cast(max(events) as bigint) as busiest_month_events
    from months
    group by user_id
)

select
    u.user_id,
    u.login,
    t.events,
    t.active_days,
    s.longest_streak_days,
    s.streak_started_on,
    s.streak_ended_on,
    t.pushes,
    t.prs_opened,
    t.reviews,
    t.issues_opened,
    t.comments,
    t.stars,
    t.forks,
    t.releases,
    t.repos_created,
    f.distinct_repos,
    f.top_repo_name,
    f.top_repo_events,
    f.peak_hour_utc,
    f.peak_hour_events,
    t.busiest_date,
    t.busiest_day_events,
    m.busiest_month,
    m.busiest_month_events,
    t.weekend_events,
    -- A share of three events is noise, not a trait.
    case
        when t.events >= {{ var('min_events_for_share_metrics') }} then t.weekend_events / t.events
    end as weekend_share,
    f.first_event_at,
    f.last_event_at,
    f.first_activity,
    f.first_repo_name
from users u
join totals t using (user_id)
join {{ ref('int_user_streaks') }} s using (user_id)
join {{ ref('int_user_favourites') }} f using (user_id)
join top_month m using (user_id)
