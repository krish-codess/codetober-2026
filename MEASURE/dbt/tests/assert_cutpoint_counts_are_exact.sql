-- Every published threshold must carry the exact number of users at or above it, counted here
-- directly with a range join rather than through buckets and a running sum.

select c.metric, c.cut_value, c.users_at_or_above, count(v.user_id) as recounted
from {{ ref('mart_metric_cutpoints') }} c
left join {{ ref('mart_metric_values') }} v
    on v.metric = c.metric and v.value >= c.cut_value
group by c.metric, c.cut_value, c.users_at_or_above
having count(v.user_id) <> c.users_at_or_above
