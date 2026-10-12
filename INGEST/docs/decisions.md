# Decisions log

What was chosen, what was rejected, and why. Newest concerns last. "Not built" entries are deliberate.

## Data and identity

**D1. The idempotency key is `(mmsi, position_timestamp)`, and the timestamp is reconstructed.**
AIS position reports carry only the second-of-minute of the fix. The key is the latest instant at or just
before the receive time whose seconds match (`validate.position_time`), so two providers hearing the same
transmission at different moments produce the same key. Rejected: keying on receive time (every provider
would create its own row) and hashing the payload (identical reports a minute apart collide).
Cost: a report delayed more than 55 seconds aliases by one minute; reports with no UTC second (2% of real
traffic) fall back to the receive second.

**D2. Same key, different position: first write wins.** Two ships sharing an MMSI, or a spoofer, produce
conflicting reports for one key. The row already stored is kept and the conflict is counted
(`ghost_positions_duplicate_total`). Rejected: last write wins, which makes replays non-deterministic.

**D3. Ancillary garbage is nulled; a missing position is quarantined.** Heading 400 or course 370 is
neither a value nor the "not available" sentinel. Dropping the whole report would lose a good position, so
the field becomes NULL. A report with no position (lat 91) has nothing worth storing and goes to quarantine.

**D4. Vessel registry: ITU MID table plus self-reported static data.** No bulk, licence-clean vessel
registry is freely available (ITU MARS has no bulk export; commercial registries are paid). Flag state
comes from the MMSI's Maritime Identification Digits (292 allocations, loaded by migration); name, call
sign, IMO, type and size come from AIS type 5/19/24 messages, with the IMO check digit verified.
Consequence: identity is what the vessel claims.

**D5. Coverage is measured, not downloaded.** Published receiver coverage maps are static, optimistic and
unavailable for most providers. The grid is computed from the positions themselves: per 0.25 degree cell,
the share of hours in which anything was heard, counting only hours in which the system heard anything at
all (so our own downtime penalises no cell). Rejected: a hand-drawn polygon per provider.
Cost: a quiet but well-covered sea lane scores as poorly covered, so dark vessels there are missed, not accused.

## Pipeline

**D6. The raw archive is the write-ahead log.** Each provider has a reader (socket to append-only file) and
a pump (file to Kafka, checkpointed). The reader never waits on Kafka. This one mechanism gives
lose-nothing-on-restart, tolerance of Kafka outages, immutable raw inputs, and replay of any past window,
all through the same decode path. Rejected: producing straight from the socket (a Kafka stall
back-pressures the provider, which then drops the connection and the data).
Flushed to the OS per line, fsynced about once a second: a process crash loses nothing, a power cut at most a second.

**D7. Collectors decode; Kafka carries decoded JSON keyed by MMSI.** Partitioning by vessel requires the
MMSI, which requires decoding before producing. JSON over Avro/Protobuf: one producer and one consumer in
one repository do not need a schema registry; the envelope carries a version field `v` for the day they do.

**D8. At-least-once delivery, effectively-once rows.** Idempotent producer with `acks=all`; the consumer
commits offsets only after the database transaction. A crash between the two redelivers a batch that the
primary key turns into a no-op. Rejected: Kafka transactions (exactly-once), which cannot span PostgreSQL anyway.

**D9. Topics.** `ais.positions` (12 partitions, key MMSI, 72 h retention), `ais.static` (12 partitions, key
MMSI, compacted), `ais.dlq` (1 partition, 14 days). One consumer group, `ghost-sink`, reads all three.
Per-vessel ordering holds within a provider; across providers it does not, which is why the live table
only moves forward (`WHERE excluded.ts > vessel_latest.ts`).

**D10. One dead-letter path, persisted.** Decode failures, validation failures and records the database
refuses all go to `ais.dlq` with the original payload, the reason and a correlation id that points at the
archive line; the sink writes them to the `quarantine` table. `ghostfleet replay-dlq --reason X`
resubmits them after a fix. A dead letter that is itself unparseable is written to the table directly:
re-queuing it would loop forever (a bug the tests found).

## Storage

**D11. `geography(Point, 4326)` for positions, not a projected CRS.** The brief asks for a projected
coordinate system appropriate to the region. The region is the planet: no projection keeps distances
honest from Singapore to Svalbard, and UTM-per-zone breaks every query that crosses a zone. Geography
computes geodesic metres on the spheroid, the SRID is explicit in the column type, and GiST indexes it.
`vessel_latest` and `coverage_cells` use `geometry(…, 4326)` because the only question asked of them is a
planar viewport box, which a geography polygon cannot express beyond a hemisphere. A regional deployment
should switch distance work to its national grid (EPSG:25833 for Norway).

**D12. A separate `vessel_latest` table.** "Latest position per vessel" from the big table is a
`DISTINCT ON` over everything. One row per vessel, upserted in the sink's transaction, makes the live map
an index scan. Cost: update churn (the table bloats between autovacuum runs).

**D13. Migrations are numbered SQL files with a 60-line runner.** Every `NNN_name.up.sql` must have a
`.down.sql` or the runner refuses to start; both directions are exercised in CI. Rejected: Alembic, which
earns its weight when there is an ORM model to diff against. There is none: every query is hand-written SQL.

**D14. Three database roles.** `ghost_ingest` can insert positions and cannot touch detections;
`ghost_detect` can read positions and owns derived tables; `ghost_api` can only SELECT. The owner
credential is used by the one-shot `init` container and nowhere else.

**D15. Not built: time partitioning of `positions`.** It is the right structure for retention and for 10x
load, and it is the first storage change to make (see performance.md). At this scale it is cost without benefit.

## Detection

**D16. A silence is "dark" only with three pieces of evidence**: reliable reception along 30 minutes of
dead-reckoned track ahead of the last position, the same looking back from the reappearance, and at least
one other vessel heard nearby during the first half hour. Anything less is a coverage gap. Rejected: a
single score threshold on gap length, which cannot tell a ship leaving range from one hiding.

**D17. Detection runs on data time, not wall-clock time.** "Now" is the newest position held. If
ingestion stalls, wall-clock reasoning would declare the entire fleet dark.

**D18. The detector is a periodic, idempotent recomputation over a lookback window**, not a stream
processor. Late data that fills a gap withdraws the event on the next pass. Rejected: detecting in the
sink at insert time, which cannot handle out-of-order arrival without the same recomputation.
Cost: gaps longer than the 72 h lookback are invisible; cost grows with window size.

**D19. The score is an explicit sum with reasons, not a model.** Each event stores the sentences that
justify its class and score. There is no labelled real data to train on, and an investigator has to be
able to argue with the output.

## API and interface

**D20. No authentication.** The system has no users, tenants or private data, and the API is read-only:
it has no mutating endpoint, runs as a SELECT-only database role, and answers 405 to every write method.
Operational actions (replay, migrate) are CLI commands that need shell access. If a write endpoint is ever
added, it needs authentication first. Rate limiting is left to the reverse proxy or CDN.

**D21. Polling with a `since` cursor, not WebSockets.** The map asks every 5 seconds for vessels updated
since the last server time. It is cacheable, stateless, trivial to load-balance and degrades to "stale"
visibly. A push channel would cut latency below the 2 second ingest budget's own noise.

**D22. No basemap tiles.** Land is a 138 kB Natural Earth outline shipped with the app. No tile provider,
no API token, no third-party request from the user's browser, and the end-to-end test runs offline.

**D23. The map is drawn outside React.** deck.gl is driven by a `requestAnimationFrame` loop over typed
arrays; React renders only the panel. This is what holds the frame rate at 20,000 vessels.

**D24. Not built: clustering, vessel-type styling, heat maps.** None is needed to find and investigate a dark vessel.

## Substitutions from the suggested stack

None in the product. For development on a machine where Docker would not start, Kafka and PostGIS were
run natively (see README, "Without Docker"); the compose stack and CI use the official images.
