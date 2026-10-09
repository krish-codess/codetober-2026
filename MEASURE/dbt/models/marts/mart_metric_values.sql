-- Long form of the rankable metrics: one row per (user, metric) where the user has a non-zero value.
-- Users at zero are still part of the population a metric is ranked against (see mart_metric_cutpoints);
-- they just cannot be "top" anything, so they need no row here.

with wide as (
    select
        user_id,
        {% for metric in rankable_metrics() %}
        cast({{ metric }} as double) as {{ metric }}{{ "," if not loop.last }}
        {% endfor %}
    from {{ ref('mart_user_year') }}
)

select user_id, metric, value
from wide
unpivot (value for metric in ({{ rankable_metrics() | join(', ') }}))
where value > 0
