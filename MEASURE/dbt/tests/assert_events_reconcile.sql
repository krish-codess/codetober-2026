-- Nothing is lost or invented between the layers. Returns a row per broken invariant.

with checks as (
    select
        'bronze lines accepted = manifest lines accepted' as invariant,
        (select count(*) from {{ source('bronze', 'events') }}) as actual,
        (select sum(lines_accepted) from {{ source('bronze', 'manifest') }}) as expected
    union all
    select
        'quarantined lines = manifest lines quarantined',
        (select count(*) from {{ source('bronze', 'quarantine') }}),
        (select sum(lines_quarantined) from {{ source('bronze', 'manifest') }})
    union all
    select
        'manifest lines read = accepted + quarantined',
        (select sum(lines_read) from {{ source('bronze', 'manifest') }}),
        (select sum(lines_accepted + lines_quarantined) from {{ source('bronze', 'manifest') }})
    union all
    select
        'deduped events = distinct event ids in bronze',
        (select count(*) from {{ ref('int_events__deduped') }}),
        (select count(distinct event_id) from {{ source('bronze', 'events') }})
    union all
    select
        'user-day events = deduped events',
        (select sum(events) from {{ ref('int_user_days') }}),
        (select count(*) from {{ ref('int_events__deduped') }})
    union all
    select
        'people events + automation events = deduped events in the year',
        (select events + automated_events from {{ ref('mart_population') }}),
        (select count(*) from {{ ref('int_events__deduped') }} where year(activity_date) = {{ var('wrapped_year') }})
    union all
    select
        'people + automated accounts = accounts active in the year',
        (select users + automated_accounts from {{ ref('mart_population') }}),
        (select count(distinct user_id) from {{ ref('int_events__deduped') }} where year(activity_date) = {{ var('wrapped_year') }})
)

select * from checks
where actual is distinct from expected
