-- Renames, and the one place raw event types are mapped onto the activities the product talks about.
-- Unknown types (upstream adds them without notice) fall through to 'other' and still count as events.
select
    event_id,
    event_type,
    case
        when event_type = 'PushEvent' then 'push'
        when event_type = 'PullRequestEvent' and action = 'opened' then 'pr_opened'
        when event_type in ('PullRequestReviewEvent', 'PullRequestReviewCommentEvent') then 'review'
        when event_type = 'IssuesEvent' and action = 'opened' then 'issue_opened'
        when event_type in ('IssueCommentEvent', 'CommitCommentEvent') then 'comment'
        when event_type = 'WatchEvent' then 'star'
        when event_type = 'ForkEvent' then 'fork'
        when event_type = 'ReleaseEvent' then 'release'
        when event_type = 'CreateEvent' and ref_type = 'repository' then 'repo_created'
        else 'other'
    end as activity,
    actor_id as user_id,
    actor_login as login,
    repo_id,
    repo_name,
    created_at as occurred_at,
    event_date as activity_date,
    batch_id,
    source_file
from {{ source('bronze', 'events') }}
