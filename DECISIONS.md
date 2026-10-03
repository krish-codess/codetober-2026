# Decisions log

What was chosen, what was rejected, and why. Newest concerns at the bottom of each section.

## Subject and scope

**The pipeline under test is dbt-labs/jaffle-shop-classic, vendored unmodified.**
"Find a real bug in an existing pipeline" is only meaningful if the pipeline was written
by someone else. A pipeline authored for this project with bugs planted in it would
prove nothing. Jaffle shop is small, widely copied as a starting point, ships its own
tests (which define the contract) and its own sample data (which defines "observed
behaviour"). Commit `fd7bfac`, Apache-2.0, under `src/tydlc/subjects/jaffle/`.

**Models are rendered with Jinja directly, not run through dbt-core.** One run executes
the pipeline thousands of times; a dbt invocation costs seconds. The models only use
`ref()` and a `for` loop, so `jinja2.Template(sql).render(ref=lambda n: n)` reproduces
them exactly and each becomes a view. Rejected: dbt-core (too slow per execution),
rewriting the SQL by hand (no longer the real pipeline).

**Two engines, same SQL.** DuckDB for speed, PostgreSQL because it is the other half of
the stack and a common dbt target. Running both turned out to be a finding in itself:
two of the four bugs appear on only one engine.

**The generator stays inside the pipeline's documented domain.** Constraints come from
jaffle's dbt tests; where they are silent, columns the models compute on are NOT NULL
with accepted values. A counterexample built from input the project never promised to
handle would be an argument, not a bug. Rejected: orphaned foreign keys and NULL
amounts (hostile, but outside the contract).

**One subject, registered in code.** `get_subject` is an `if`. A plugin registry,
YAML-defined subjects and a dbt `schema.yml` parser were all skipped: there is one
pipeline. The seam exists (`Subject` dataclass) and that is enough until a second one
shows up.

## Generation and shrinking

**Hypothesis generates and shrinks; a 20-line pass guarantees row minimality.**
Hypothesis's shrinker does the heavy lifting. Because foreign keys are drawn as an
index into the parent rows, removing a parent sometimes needs a coordinated change the
shrinker does not find, and results had a spare row. `_drop_rows` removes any row
(cascading to its children) while the failure persists, so the claim "removing any row
makes the property hold" is true by construction. Rejected: a custom shrinker
(reinventing Hypothesis), trusting Hypothesis alone (measurably non-minimal).

**Foreign keys are indexes, uniqueness is post-hoc.** Building `sampled_from(parents)`
inside the draw, and `unique_by` on the row lists, made generation 18 ms per dataset
and left the shrinker stuck on non-minimal lists. Static strategies plus "drop later
duplicates" brought it to 5 ms and fixed the minimality. Measured in
`docs/performance.md`.

**Half of all references go to the first parent.** Uniform references almost never
produce three payments on one order, which is what the float-ordering bug needs: it
was missed entirely at 80 examples. With a hot key it is found within 40.

**NUL and lone surrogates are excluded from generated text.** PostgreSQL `text` cannot
store them; including them tests the driver, not the pipeline.

**The sweep never stops at the first failure.** A normal Hypothesis test stops at the
first counterexample, which makes "how often does this fail" unanswerable. The sweep
judges every example against every property and records all verdicts; shrinking is a
separate `hypothesis.find` per falsified property.

**`Phase.explain` is disabled when shrinking.** It was 95% of shrink time (30 s of
32 s) and its output is not used.

**Shrinks run in a process pool (spawn).** They are independent and CPU-bound. Spawn
rather than fork so that connections and the API's threads are never forked, and so
Linux and Windows behave the same.

## Properties, discovery, gate

**Discovery is template enumeration over the seed data (Daikon style).** Seven
templates (`not_null`, `unique`, `non_negative`, `le`, `subset`, `sum_eq`,
`row_count_eq`), instantiated over every model column, kept if they hold on jaffle's
real sample. Each survivor is then run as an ordinary property. Rejected: learning
invariants from generated data (circular: it would learn the generator), ML-based
inference (unexplainable candidates).

**Confidence is the rule of three: `1 - 3/n`.** After n examples that exercised an
invariant without a violation, the true violation rate is below 3/n at 95% confidence.
It is honest about small n (2 passes = confidence 0) and never reaches 1. Falsified
means 0. Rejected: pass ratio (a property that failed once is not "97% true").

**Hit rate = candidates that survive / candidates proposed.** It measures how much of
what the clean sample suggests is actually true. It does not measure how many
survivors are *meaningful*; that needs a human, and the limitations say so.

**Known bugs are a baseline in code, enforced both ways.** CI must be green while four
real bugs exist in a pipeline we do not own. The gate fails on a declared property
that fails without being listed, and on a listed one that stops failing. Discovered
candidates never gate: they are hypotheses.

**`deterministic` compares floats to three decimals.** Bit-level run-to-run drift on
DuckDB is real (finding 4) but depends on engine history, so no seed reproduces it and
it cannot gate. The tolerant property gates; the drift is reproduced by a non-strict
xfail test.

## Storage

**Plain SQL migrations with a 25-line runner, not Alembic.** One `NNNN_name.up.sql` and
`.down.sql` per change, each applied in a transaction with its bookkeeping row.
Alembic's value is autogeneration from ORM models and there is no ORM here. Rollback
is exercised by a test and was performed by hand (README).

**`minimal_dataset` is JSON, not JSONB.** JSONB reorders object keys; the viewer
then showed tables and columns in a different order from the schema. Nothing queries
inside the document. Found by the browser test.

**Results in PostgreSQL, per-example verdicts in Parquet.** The summary is relational
and small; the verdict log is `examples x properties` rows per run, append-only and
only ever scanned, which is what Parquet and DuckDB are for. The per-property tallies
in every report are computed by DuckDB from that file.

**Quarantine is Parquet next to the staged data, not a database table.** The ingest
stage must work with no database running.

**One connection per request, no pool.** Marked `ponytail:` in `api.py`; it is the
first thing to change under load (see performance doc).

## API and frontend

**API key on the one mutating endpoint; reads are open.** The system has no users or
tenants. The key (constant-time compare, from the environment) stops strangers from
burning CPU. With no key configured, nobody can start runs. Rejected: OAuth/JWT (no
identity to carry).

**Runs started over HTTP execute in a FastAPI background task.** No queue or worker
service. The cost: a run dies with the process, which is why runs left `running` for
an hour are marked failed at startup. Rejected: Celery/Redis for one job type.

**No frontend framework and no build step.** Two views and a detail page. One ES
module, imported as-is by `node --test`, so the tested code is the shipped code.
Rejected: React + bundler (a toolchain to maintain for ~300 lines).

**Strings in the minimal dataset are shown JSON-quoted.** Generated data contains
`""`, `" "`, `"NULL"` and real NULL; unquoted they are indistinguishable.

**Host ports default to 8028 and 5438.** 8000 and 5432 were both already taken on the
development machine, by another project and by a local PostgreSQL that silently
answered instead of the container.

## Deliberately not built

- A dbt adapter / `schema.yml` parser: one subject, constraints transcribed by hand.
- Parallel sweep: the sweep is ~30% of run time; shrinking was the bottleneck.
- Value-level minimality proof: rows are 1-minimal by construction, values are
  whatever Hypothesis converged on (in practice 0, NULL, 1, -1).
- Connection pool, job queue, multi-tenant auth: see above.
- Publishing to PyPI/GHCR: the workflow is written, but needs the owner's accounts.
