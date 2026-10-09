{{ config(materialized='incremental', incremental_strategy='delete+insert', unique_key='activity_date') }}

-- One row per user per UTC day they did anything. Same late-data strategy as int_events__deduped:
-- any day that received an event from a newer batch is rebuilt whole.

with events as (
    select * from {{ ref('int_events__deduped') }}
)

{% if is_incremental() %}
, affected_days as (
    select distinct activity_date
    from events
    where batch_id > (select coalesce(max(max_batch_id), 0) from {{ this }})
)
{% endif %}

select
    user_id,
    activity_date,
    count(*) as events,
    count(*) filter (where activity = 'push') as pushes,
    count(*) filter (where activity = 'pr_opened') as prs_opened,
    count(*) filter (where activity = 'review') as reviews,
    count(*) filter (where activity = 'issue_opened') as issues_opened,
    count(*) filter (where activity = 'comment') as comments,
    count(*) filter (where activity = 'star') as stars,
    count(*) filter (where activity = 'fork') as forks,
    count(*) filter (where activity = 'release') as releases,
    count(*) filter (where activity = 'repo_created') as repos_created,
    max(batch_id) as max_batch_id
from events
{% if is_incremental() %}
where activity_date in (select activity_date from affected_days)
{% endif %}
group by user_id, activity_date
