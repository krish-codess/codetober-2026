# CODETOBER 2026

One complete project shipped every day of October 2026. 31 days, 31 projects.

Each folder is one day's project and stands alone: its own dependencies, `.env.example`,
container build, tests and docs. Nothing is shared between folders, so you can read or run
any one of them without the others.

## Projects

| Folder | Project | Stack |
|---|---|---|
| [EMIT](EMIT/) | **The Last Ticket**: an on-sale ticketing system with a virtual waiting room, event-sourced inventory and expiring holds, built so nothing is oversold | Java 17, Spring Boot, PostgreSQL, Kafka, Redis, React, TypeScript, Kubernetes |
| [PARSE](PARSE/) | Hierarchical, multilingual feedback classification with active learning | Python, FastAPI, scikit-learn, ONNX Runtime, PostgreSQL, React, TypeScript |
| [TRANSFORM](TRANSFORM/) | **Gold Standard**: a consumer price index for video game economies, on real EVE Online market data and a simulated shard | Python, Dagster, PostgreSQL, React |
| [VALIDATE](VALIDATE/) | **tydlc**: property-based testing for data pipelines, with failures shrunk to a minimal dataset | Python, PostgreSQL, DuckDB |
| [SHIP](SHIP/) | **shipd**: a zero-downtime schema migration controller. Plans a diff against the live schema, applies it as expand/contract with lock guards and auto-revert, while two application versions keep serving traffic | Go, pgroll, PostgreSQL, React, TypeScript, Prometheus, Grafana, Kubernetes |
| [MEASURE](MEASURE/) | **Your App, Wrapped**: a personalised year-in-review for every user where every percentile claim is exactly true, audited before publishing | Python, dbt, DuckDB, FastAPI, PostgreSQL, React, TypeScript |
| [LAKE](LAKE/) | **The Compression Bake-Off**: one dataset written as 24 variants across CSV, Parquet, ORC and Avro, measured in bytes actually read and turned into monthly cloud cost | Python, PyArrow, DuckDB, Polars, FastAPI, React, TypeScript |
| [VISION](VISION/) | **squeeze**: compress a vision model for edge boards (pruning, distillation, INT8) and measure the real accuracy, latency and power tradeoff on the device | Python, PyTorch, ONNX Runtime, OpenVINO, FastAPI, SQLite, React, TypeScript |

## Running a project

Every project runs with Docker and Compose v2:

```bash
cd <FOLDER>
cp .env.example .env    # replace any CHANGE_ME values
docker compose up --build
```

Ports, URLs, non-Docker options and measured results are in each project's own README.
SHIP and VISION do not have one yet: start from `docker-compose.yml` and `.env.example`,
and see `docs/` in each for the API reference, decisions and evidence.

## Inside a project folder

The layout repeats, with small differences per stack:

| Path | What is there |
|---|---|
| `README.md` | What it is, how to run it, measured results |
| `DECISIONS.md` or `docs/` | What was chosen and rejected and why, data dictionary, generated OpenAPI, screenshots |
| `.env.example` | Every setting, with placeholder values only |
| `docker-compose.yml`, `Dockerfile`, `deploy/` | Local stack and deployment manifests |
| `tests/` | Unit, integration and end-to-end tests |
| `results/`, `reports/`, `docs/evidence/` | Committed measurements the README numbers come from |

## CI

GitHub only runs workflows from the repository root, so each project's workflow lives in
[.github/workflows](.github/workflows/) and is scoped to its folder with `paths:`.
MEASURE and VISION are set up this way. The workflows inside `EMIT/`, `PARSE/`,
`TRANSFORM/` and `VALIDATE/` came along when those projects were imported with their
history; they document how each was tested but do not run here.
