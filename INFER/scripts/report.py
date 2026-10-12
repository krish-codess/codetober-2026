"""Write the measured results into docs/evidence/: model-report.md and timings.md. Nothing in them is typed by hand.

python scripts/report.py            (uses UPLIFT_MIGRATE_DATABASE_URL and the data directory's run_report.jsonl)
"""

from __future__ import annotations

import json
import platform
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from uplift import config, policy

DOCS = Path(__file__).resolve().parent.parent / "docs" / "evidence"
ORDER = ["x_learner", "t_learner", "s_learner", "causal_forest", "response", "random"]


def pts(v: float | None, digits: int = 2) -> str:
    return "-" if v is None else f"{v * 100:+.{digits}f}"


def model_report(conn: psycopg.Connection[dict[str, Any]]) -> str:
    out = [
        "# Model report",
        "",
        "Written by `python scripts/report.py` from the seeded database. Do not edit by hand.",
        "",
        "Qini is the area between a model's Qini curve and random targeting, in outcome per treated unit, shown x100.",
        "Intervals are 95% from a paired bootstrap of the test split. Uplift is in percentage points of visit rate.",
        "",
    ]
    experiments = conn.execute(
        "SELECT DISTINCT ON (d.name) e.*, d.name AS dataset FROM experiment e JOIN dataset d ON d.id = e.dataset_id"
        " WHERE e.status = 'complete' ORDER BY d.name DESC, e.finished_at DESC"
    ).fetchall()
    for e in experiments:
        audit, ref = e["audit"], e["audit"]["reference"]
        runs = {
            r["learner"]: r
            for r in conn.execute("SELECT * FROM model_run WHERE experiment_id = %s", [e["id"]]).fetchall()
        }
        champion = next(r for r in runs.values() if r["is_champion"])
        synthetic = ref["oracle_qini_test"] is not None
        out += [
            f"## {e['dataset']}",
            "",
            f"- {audit['units']:,} units; split `{e['split_strategy']}` into {ref['split_sizes']['train']:,} train /"
            f" {ref['split_sizes']['val']:,} validation / {ref['split_sizes']['test']:,} test",
            f"- Randomization: {audit['treated_share']:.4f} treated against a design of {audit['expected_treated_share']:.4f}"
            f" (sample-ratio p = {audit['srm_p_value']:.2f}); largest standardized mean difference"
            f" {audit['max_standardized_mean_difference']:.4f}; features predict treatment with AUC {audit['treatment_auc']:.3f}",
            f"- Average effect on the test split: {pts(ref['ate_test']['uplift'])} pts"
            f" ({pts(ref['ate_test']['ci_low'])} to {pts(ref['ate_test']['ci_high'])})",
            f"- Champion (best validation Qini among uplift learners): **{champion['learner']}**",
            f"- Code `{e['code_version']}`, data version `{e['data_version'][:12]}`, seed {e['params']['seed']}",
            "",
            "| learner | val Qini | test Qini | 95% interval | beats `response` | uplift top 10% | top 20% | top 30% | calibration slope |"
            + (" share of oracle | PEHE |" if synthetic else ""),
            "|---|---:|---:|---|---:|---:|---:|---:|---:|" + ("---:|---:|" if synthetic else ""),
        ]
        for name in ORDER:
            r = runs.get(name)
            if r is None or r["status"] != "complete":
                continue
            t, v = r["metrics"]["test"], r["metrics"]["val"]
            slope = r["metrics"]["calibration_slope"]
            row = (
                f"| {name}{' (champion)' if r['is_champion'] else ''} | {v['qini'] * 100:.3f} | {t['qini'] * 100:.3f}"
                f" | {t['qini_ci95'][0] * 100:.3f} to {t['qini_ci95'][1] * 100:.3f}"
                f" | {'-' if 'p_beats_response' not in t else format(t['p_beats_response'], '.0%')}"
                f" | {pts(t['uplift_at']['0.1'], 1)} | {pts(t['uplift_at']['0.2'], 1)} | {pts(t['uplift_at']['0.3'], 1)}"
                f" | {'-' if slope is None else format(slope, '.2f')} |"
            )
            if synthetic:
                row += f" {t['qini_share_of_oracle']:.0%} | {'-' if 'pehe' not in t else format(t['pehe'], '.4f')} |"
            out.append(row)
        out.append("")

        out += ["Deciles of the champion's predicted uplift, test split (1 = treat first):", ""]
        out += [
            "| decile | predicted | observed | 95% interval |" + (" true |" if synthetic else ""),
            "|---:|---:|---:|---|" + ("---:|" if synthetic else ""),
        ]
        for d in conn.execute("SELECT * FROM decile WHERE run_id = %s ORDER BY decile", [champion["id"]]).fetchall():
            out.append(
                f"| {d['decile']} | {pts(d['mean_predicted'], 1)} | {pts(d['observed_uplift'], 1)} | {pts(d['ci_low'], 1)} to {pts(d['ci_high'], 1)} |"
                + (f" {pts(d['true_uplift'], 1)} |" if synthetic else "")
            )
        out.append("")

        hurts = conn.execute(
            "SELECT * FROM segment WHERE experiment_id = %s AND verdict = 'hurts' ORDER BY uplift", [e["id"]]
        ).fetchall()
        by_verdict = "SELECT verdict, count(*) AS n FROM segment WHERE experiment_id = %s GROUP BY verdict"
        counts = {r["verdict"]: r["n"] for r in conn.execute(by_verdict, [e["id"]]).fetchall()}
        out += [
            f"Segment scan (model-free, whole experiment): {sum(counts.values())} segments tested,"
            f" {counts.get('helps', 0)} help, {counts.get('hurts', 0)} hurt, {counts.get('unclear', 0)} unclear after correction.",
            "",
        ]
        if hurts:
            out += [
                "| segment it hurts | uplift | 95% interval | adjusted p | treated / control |",
                "|---|---:|---|---:|---:|",
            ]
            out += [
                f"| {s['definition']} | {pts(s['uplift'], 1)} | {pts(s['ci_low'], 1)} to {pts(s['ci_high'], 1)} | {s['p_adjusted']:.2g} | {s['n_treated']:,} / {s['n_control']:,} |"
                for s in hurts
            ]
        else:
            lowest = conn.execute(
                "SELECT * FROM segment WHERE experiment_id = %s ORDER BY uplift LIMIT 1", [e["id"]]
            ).fetchone()
            assert lowest is not None
            out.append(
                f"No segment is reliably harmed. The lowest observed uplift is {pts(lowest['uplift'], 1)} pts"
                f" ({pts(lowest['ci_low'], 1)} to {pts(lowest['ci_high'], 1)}) in `{lowest['definition']}`, verdict `{lowest['verdict']}`."
            )
        out.append("")

        negative = champion["profiles"].get("negative_uplift")
        if negative:
            traits = "; ".join(
                f"{t['feature']} = {t['level']} ({t['share_in_group']:.0%} vs {t['share_overall']:.0%} overall)"
                for t in negative["traits"][:3]
            )
            out += [
                f"Units the champion predicts are harmed (test split): {negative['n']:,} ({negative['share_of_population']:.1%});"
                f" observed uplift {pts(negative['observed_uplift'], 1)} pts ({pts(negative['ci_low'], 1)} to {pts(negative['ci_high'], 1)}),"
                f" verdict `{negative['verdict']}`"
                + (f"; true effect {pts(negative['true_uplift'], 1)} pts" if synthetic else "")
                + f". Traits: {traits}.",
                "",
            ]
        else:
            out += ["The champion predicts harm for too few held-out units to profile (fewer than 100).", ""]

        out += [
            "Budgeted policies (value 20 per visit, cost 1 per treatment), valued from held-out experiment outcomes, profit per 1,000 customers:",
            "",
            "| budget share | treated | policy | treat all | treat none | policy - all (95%) | policy - none (95%) | effect captured |"
            + (" true visits: policy / oracle / all |" if synthetic else ""),
            "|---:|---:|---:|---:|---:|---|---|---:|" + ("---|" if synthetic else ""),
        ]
        population = ref["split_sizes"]["test"]
        for share in (0.1, 0.2, 0.3, 0.5, 1.0):
            budget = Decimal(round(population * share))
            p = policy.create(conn, champion["id"], budget, Decimal(1), Decimal(20), key=f"report-{e['id']}-{share}")
            ev = p["evaluation"]
            if not ev["evaluable"]:
                continue

            def diff(d: dict[str, Any]) -> str:
                return f"{d['estimate'] * 1000:+.0f} ({d['ci95'][0] * 1000:+.0f} to {d['ci95'][1] * 1000:+.0f})"

            row = (
                f"| {share:.0%} | {p['n_targeted']:,} ({p['n_targeted'] / population:.0%}) | {ev['profit_per_unit']['policy'] * 1000:,.0f}"
                f" | {ev['profit_per_unit']['treat_all'] * 1000:,.0f} | {ev['profit_per_unit']['treat_none'] * 1000:,.0f}"
                f" | {diff(ev['policy_minus_treat_all'])} | {diff(ev['policy_minus_treat_none'])} | {ev['share_of_treat_all_effect_captured']:.0%} |"
            )
            if synthetic:
                tr = ev["true_incremental_outcomes_per_unit"]
                row += (
                    f" {tr['policy'] * 1000:.1f} / {tr['oracle_same_size'] * 1000:.1f} / {tr['treat_all'] * 1000:.1f} |"
                )
            out.append(row)
        out += [
            "",
            "With a budget share of 100% the policy still stops where predicted uplift x 20 falls to the cost of 1.",
            "",
        ]
    conn.commit()
    return "\n".join(out)


def timings(settings: config.Settings, conn: psycopg.Connection[dict[str, Any]]) -> str:
    out = [
        "# Measured timings",
        "",
        "Written by `python scripts/report.py` from the run report of the last `uplift seed`. Do not edit by hand.",
        "",
        f"Machine: {platform.processor() or platform.machine()}, {platform.system()} {platform.version()}, Python {platform.python_version()},"
        " PostgreSQL 18 on the same host. One process; the forest uses 2 threads.",
        "",
        "| stage | seconds | volume | throughput |",
        "|---|---:|---|---:|",
    ]
    stages = [json.loads(line) for line in (settings.data_dir / "run_report.jsonl").read_text().splitlines()]
    for s in stages:
        r = s["result"] or {}
        if "rows_total" in r:
            volume, rate = (
                f"{r['rows_total']:,} rows ({r['rows_quarantined']:,} quarantined)",
                f"{r['rows_total'] / s['seconds']:,.0f} rows/s",
            )
        elif "rows_main" in r:
            volume, rate = (
                f"{r['rows_main'] + r['rows_late']:,} rows written",
                f"{(r['rows_main'] + r['rows_late']) / s['seconds']:,.0f} rows/s",
            )
        elif "rows" in r:
            volume, rate = f"{r['rows']:,} units scored", f"{r['rows'] / s['seconds']:,.0f} rows/s"
        elif "units" in r:
            volume, rate = f"{r['units']:,} units, 6 learners", "-"
        else:
            volume, rate = "-", "-"
        out.append(f"| {s['stage']} | {s['seconds']:.1f} | {volume} | {rate} |")
    out += ["", "Inside training:", "", "| dataset | " + " | ".join(["stage", "seconds"]) + " |", "|---|---|---:|"]
    for e in conn.execute(
        "SELECT DISTINCT ON (d.name) d.name, e.timings FROM experiment e JOIN dataset d ON d.id = e.dataset_id"
        " WHERE e.status = 'complete' ORDER BY d.name DESC, e.finished_at DESC"
    ).fetchall():
        out += [
            f"| {e['name']} | {stage} | {seconds:.1f} |"
            for stage, seconds in sorted(e["timings"].items(), key=lambda kv: -kv[1])
        ]
    return "\n".join(out) + "\n"


def main() -> None:
    settings = config.load()
    DOCS.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(settings.migrate_database_url, row_factory=dict_row) as conn:
        (DOCS / "model-report.md").write_text(model_report(conn), encoding="utf-8")
        (DOCS / "timings.md").write_text(timings(settings, conn), encoding="utf-8")
    print("wrote", DOCS / "model-report.md", "and", DOCS / "timings.md")


if __name__ == "__main__":
    main()
