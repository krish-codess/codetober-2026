-- One row: what everyone did together. Only sums over the whole population, so nothing here is about anyone.

select
    {{ var('wrapped_year') }} as year,
    count(*) as users,
    cast(sum(events) as bigint) as events,
    cast(sum(pushes) as bigint) as pushes,
    cast(sum(prs_opened) as bigint) as prs_opened,
    cast(sum(reviews) as bigint) as reviews,
    cast(sum(stars) as bigint) as stars,
    cast(sum(active_days) as bigint) as active_days,
    (select count(*) from {{ ref('int_users') }} where is_automated) as automated_accounts,
    (select cast(coalesce(sum(events), 0) as bigint) from {{ ref('int_users') }} where is_automated) as automated_events
from {{ ref('mart_user_year') }}
