{# The metrics a user can be ranked on. Each is a column of mart_user_year. #}
{% macro rankable_metrics() %}
    {{ return([
        'events', 'active_days', 'longest_streak_days', 'busiest_day_events', 'distinct_repos',
        'pushes', 'prs_opened', 'reviews', 'issues_opened', 'comments', 'stars', 'forks', 'releases',
        'weekend_share',
    ]) }}
{% endmacro %}

{# Quantiles asked of each sketch: every percent, then finer steps into the top tail. #}
{% macro quantile_ladder() %}
    {% set ladder = [] %}
    {% for i in range(1, 100) %}{% do ladder.append(i / 100) %}{% endfor %}
    {% do ladder.extend([0.995, 0.998, 0.999, 0.9995, 0.9999]) %}
    {{ return(ladder) }}
{% endmacro %}
