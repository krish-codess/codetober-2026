-- Distribution invariants on the per-user row. A pipeline that runs green and produces a user with
-- a 400-day streak or more pushes than events has to fail here.

select *
from {{ ref('mart_user_year') }}
where events < 1
   or active_days not between 1 and 366
   or active_days > events
   or longest_streak_days not between 1 and active_days
   or datediff('day', streak_started_on, streak_ended_on) + 1 <> longest_streak_days
   or pushes + prs_opened + reviews + issues_opened + comments + stars + forks + releases + repos_created > events
   or busiest_day_events > events
   or busiest_month_events not between busiest_day_events and events
   or coalesce(top_repo_events, 0) > events
   or distinct_repos > events
   or peak_hour_utc not between 0 and 23
   or busiest_month not between 1 and 12
   or weekend_events > events
   or weekend_share not between 0 and 1
   or year(first_event_at) <> {{ var('wrapped_year') }}
   or year(busiest_date) <> {{ var('wrapped_year') }}
   or login like '%[bot]'
