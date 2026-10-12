"""Command line. Every pipeline stage is its own command, idempotent, and runnable on its own."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from uplift import config, db
from uplift.config import Settings, log, warn

logger = logging.getLogger("uplift.cli")
REPO_ROOT = Path(__file__).resolve().parent.parent


def _timed(settings: Settings, stage: str, fn: Callable[[], Any]) -> Any:
    """Run a stage, log its duration, and append it to the run report the committed measurements are read from."""
    started = time.perf_counter()
    result = fn()
    seconds = round(time.perf_counter() - started, 2)
    log(logger, "stage finished", stage=stage, seconds=seconds)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / "run_report.jsonl").open("a") as fh:
        fh.write(json.dumps({"stage": stage, "seconds": seconds, "result": result}, default=str) + "\n")
    return result


def migrate(settings: Settings, revision: str = "head") -> None:
    direction = "downgrade" if revision.startswith("-") or revision == "base" else "upgrade"
    subprocess.run([sys.executable, "-m", "alembic", direction, revision], check=True, cwd=REPO_ROOT)  # noqa: S603 - fixed argv


def seed(settings: Settings, units: int, hillstrom: bool, seed_value: int) -> None:
    """Everything a fresh stack needs: both datasets ingested, models trained, champion scored. Safe to re-run:
    files already ingested are skipped, and a dataset whose latest experiment matches its data is not retrained."""
    from uplift import ingest, synth, train

    out = settings.data_dir / "generated"
    _timed(settings, "generate", lambda: synth.generate(out, units, seed_value))
    _timed(settings, "ingest synthetic", lambda: ingest.ingest(settings, out / "synthetic.csv", "synthetic"))
    _timed(
        settings,
        "ingest synthetic (late file)",
        lambda: ingest.ingest(settings, out / "synthetic_late.csv", "synthetic"),
    )
    datasets = ["synthetic"]
    if hillstrom:
        try:
            path = ingest.fetch_hillstrom(settings)
        except SystemExit as unavailable:
            # The real dataset is an external dependency. Without it the system still comes up, on synthetic data.
            warn(logger, "continuing without the Hillstrom dataset", reason=str(unavailable))
        else:
            _timed(settings, "ingest hillstrom", lambda: ingest.ingest(settings, path, "hillstrom"))
            datasets.append("hillstrom")
    for name in datasets:
        with db.connect(settings.batch_database_url) as conn:
            current = conn.execute(
                "SELECT e.data_version, e.dataset_id FROM experiment e JOIN dataset d ON d.id = e.dataset_id"
                " WHERE d.name = %s AND e.status = 'complete' ORDER BY e.finished_at DESC LIMIT 1",
                [name],
            ).fetchone()
            fresh = current is not None and current["data_version"] == db.data_version(conn, current["dataset_id"])
        if not fresh:
            _timed(settings, f"train {name}", lambda name=name: train.train(settings, name))  # type: ignore[misc]
        _timed(settings, f"score {name}", lambda name=name: train.score(settings, name))  # type: ignore[misc]


def export_policy(
    settings: Settings, dataset: str, budget: Decimal, cost: Decimal, value: Decimal, out: Path
) -> dict[str, Any]:
    """Create a policy for the dataset's current champion and write the activation file: one unit id per line."""
    from uplift import policy

    with db.connect(settings.batch_database_url) as conn:
        run = conn.execute(
            "SELECT r.id FROM model_run r JOIN experiment e ON e.id = r.experiment_id JOIN dataset d ON d.id = e.dataset_id"
            " WHERE d.name = %s AND e.status = 'complete' AND r.is_champion ORDER BY e.finished_at DESC LIMIT 1",
            [dataset],
        ).fetchone()
        if run is None:
            raise SystemExit(f"no complete experiment for dataset {dataset!r}")
        try:
            p = policy.create(conn, run["id"], budget, cost, value, key=f"cli-{uuid.uuid4()}")
        except policy.NotScored as exc:
            raise SystemExit(str(exc)) from exc
        rows = conn.execute(policy.EXPORT, [run["id"], p["n_targeted"]]).fetchall()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["unit_id", "predicted_uplift"])
        writer.writerows((r["unit_id"], f"{r['uplift']:.6f}") for r in rows)
    return {
        "policy": str(p["id"]),
        "file": str(out),
        "n_targeted": p["n_targeted"],
        "population": p["population"],
        "evaluation": p["evaluation"],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="uplift", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="apply database migrations (alembic upgrade head), or downgrade with e.g. -1")
    p.add_argument("revision", nargs="?", default="head")

    p = sub.add_parser("generate", help="write a synthetic experiment with a known effect and realistic defects")
    p.add_argument("--units", type=int, default=200_000)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--out", type=Path)

    sub.add_parser("fetch", help="download the Hillstrom file (retries, verifies its SHA-256)")

    p = sub.add_parser("ingest", help="validate a raw file into units and quarantine")
    p.add_argument("file", type=Path)
    p.add_argument("--dataset", required=True)
    p.add_argument("--outcome", default="visit", choices=["visit", "conversion", "spend"])

    p = sub.add_parser("profile", help="profile a raw file against the ingestion contract (markdown on stdout)")
    p.add_argument("file", type=Path)

    p = sub.add_parser("train", help="train and compare every learner on a dataset")
    p.add_argument("--dataset", required=True)
    p.add_argument("--learners", nargs="+")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument(
        "--treated-share", type=float, default=2 / 3, help="the share the experiment design assigned to treatment"
    )

    p = sub.add_parser("score", help="batch-score every unit with the champion model")
    p.add_argument("--dataset", required=True)

    p = sub.add_parser("policy", help="create a budgeted targeting policy and export the activation file")
    p.add_argument("--dataset", required=True)
    p.add_argument("--budget", type=Decimal, required=True)
    p.add_argument("--cost", type=Decimal, required=True, help="cost of treating one unit")
    p.add_argument("--value", type=Decimal, required=True, help="value of one outcome")
    p.add_argument("--out", type=Path, required=True)

    p = sub.add_parser("retire", help="stop serving an experiment; the previous complete one is served again")
    p.add_argument("experiment")

    p = sub.add_parser("seed", help="generate, ingest, train and score everything a fresh stack needs")
    p.add_argument("--units", type=int, default=200_000)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--no-hillstrom", action="store_true", help="skip the real dataset (no network needed)")

    p = sub.add_parser("serve", help="run the API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("openapi", help="write the API reference generated from the code")
    p.add_argument("out", type=Path)

    args = parser.parse_args(argv)
    settings = config.load()
    config.setup_logging(settings.log_level)
    config.correlation_id.set(f"{args.command}-{uuid.uuid4().hex[:8]}")

    result: Any = None
    if args.command == "migrate":
        migrate(settings, args.revision)
    elif args.command == "generate":
        from uplift import synth

        result = synth.generate(args.out or settings.data_dir / "generated", args.units, args.seed)
    elif args.command == "fetch":
        from uplift import ingest

        result = {"file": str(ingest.fetch_hillstrom(settings))}
    elif args.command == "ingest":
        from uplift import ingest

        result = _timed(
            settings, f"ingest {args.dataset}", lambda: ingest.ingest(settings, args.file, args.dataset, args.outcome)
        )
    elif args.command == "profile":
        from uplift import profile

        sys.stdout.write(profile.profile(args.file))
    elif args.command == "train":
        from uplift import train

        result = _timed(
            settings,
            f"train {args.dataset}",
            lambda: train.train(settings, args.dataset, args.learners, args.seed, treated_share=args.treated_share),
        )
    elif args.command == "score":
        from uplift import train

        result = _timed(settings, f"score {args.dataset}", lambda: train.score(settings, args.dataset))
    elif args.command == "policy":
        result = export_policy(settings, args.dataset, args.budget, args.cost, args.value, args.out)
    elif args.command == "retire":
        with db.connect(settings.batch_database_url) as conn:
            done = conn.execute(
                "UPDATE experiment SET status = 'retired' WHERE id = %s AND status = 'complete'", [args.experiment]
            ).rowcount
        if not done:
            raise SystemExit("no complete experiment with that id")
        result = {"retired": args.experiment}
    elif args.command == "seed":
        seed(settings, args.units, not args.no_hillstrom, args.seed)
    elif args.command == "serve":
        import uvicorn

        uvicorn.run("uplift.api:app", host=args.host, port=args.port, log_config=None)
    elif args.command == "openapi":
        from uplift.api import app

        args.out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
    if result is not None:
        print(json.dumps(result, indent=2, default=str))
