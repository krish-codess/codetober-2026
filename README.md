# CODETOBER 2026

31 projects.
31 days.
One shipped project every day of October.

## What this is

Codetober is a month-long challenge: build and ship one complete project every day of October 2026.
This repository holds the source code for all of them.

## Layout

One folder per project, each self-contained with its own dependencies, `.env.example`, and Docker setup.
Nothing is shared between folders.

## Projects

| Folder | Project | Stack |
|---|---|---|
| [EMIT](EMIT/) | **The Last Ticket**: an on-sale ticketing system with a virtual waiting room, event-sourced inventory and expiring holds, built so nothing is oversold | Java 17, Spring Boot, PostgreSQL, Kafka, Redis, React, TypeScript, Kubernetes |
| [PARSE](PARSE/) | Hierarchical, multilingual feedback classification with active learning | Python, FastAPI, scikit-learn, ONNX Runtime, PostgreSQL, React, TypeScript |
| [TRANSFORM](TRANSFORM/) | **Gold Standard**: a consumer price index for video game economies, on real EVE Online market data and a simulated shard | Python, Dagster, PostgreSQL, React |
| [VALIDATE](VALIDATE/) | **tydlc**: property-based testing for data pipelines, with failures shrunk to a minimal dataset | Python, PostgreSQL, DuckDB |

## Running a project

Every project so far runs with Docker and Compose v2:

```bash
cd <FOLDER>
cp .env.example .env
docker compose up --build
```

Ports, URLs and non-Docker options are in each project's own README.
