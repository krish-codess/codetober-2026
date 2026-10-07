{{ config(materialized='incremental', incremental_strategy='delete+insert', unique_key='activity_date') }}

-- One row per event. Upstream redelivers events, usually within the hour, sometimes days later.
--
-- Late-arriving data: an incremental run finds the calendar days touched by batches newer than
-- anything already loaded, then rebuilds those whole days from bronze (delete+insert on
-- activity_date). Rebuilding the day, rather than appending the new rows, is what keeps a
-- redelivered event from being counted twice when its copies arrive in different batches.
-- The day filter is pushed into the Parquet scan, so only matching row groups are read.

with source as (
    select * from {{ ref('stg_gharchive__events') }}
)

{% if is_incremental() %}
, affected_days as (
    select distinct activity_date
    from source
    where batch_id > (select coalesce(max(batch_id), 0) from {{ this }})
)
{% endif %}

select *
from source
{% if is_incremental() %}
where activity_date in (select activity_date from affected_days)
{% endif %}
-- Copies of an event are identical, so which one survives does not matter; the earliest batch is stable.
qualify row_number() over (partition by event_id order by batch_id, source_file) = 1
