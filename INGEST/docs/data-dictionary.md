# Data dictionary

PostgreSQL with PostGIS. Schema: `ghostfleet/migrations/001_schema.up.sql`. All timestamps are
`timestamptz` and UTC; all coordinates are WGS 84 (SRID 4326), longitude then latitude.

## `vessels`: one row per ship-station MMSI ever seen

| column | type | null | meaning |
|---|---|---|---|
| `mmsi` | integer, PK | no | Maritime Mobile Service Identity. CHECK 200000000-799999999 (ship stations only) |
| `mid` | smallint, generated | no | First three digits of the MMSI: the country allocation. Joins to `mid_countries`; deliberately not a foreign key, because real feeds contain unallocated MIDs |
| `imo` | integer | yes | IMO ship number, 7 digits. Stored only if its check digit is valid |
| `name` | text | yes | Self-reported name, upper case as transmitted |
| `callsign` | text | yes | Self-reported radio call sign |
| `ship_type` | smallint | yes | AIS ship type code 1-255 (30 fishing, 60-69 passenger, 70-79 cargo, 80-89 tanker). 0 on the wire is stored as NULL |
| `length_m` | smallint | yes | Metres; bow plus stern antenna offsets |
| `beam_m` | smallint | yes | Metres; port plus starboard antenna offsets |
| `draught_m` | real | yes | Metres, self-reported maximum static draught |
| `destination` | text | yes | Free text typed by the crew |
| `first_seen` | timestamptz | no | When this system first stored the vessel (wall clock) |
| `static_updated_at` | timestamptz | yes | Receive time of the newest static report merged in |

Static fields merge: a new report fills blanks and overwrites values, but a blank never erases a known value.

## `positions`: every accepted position report

| column | type | null | meaning |
|---|---|---|---|
| `mmsi` | integer, FK `vessels` | no | |
| `ts` | timestamptz | no | When the position was fixed, to the second, reconstructed from the report's second-of-minute (decisions D1) |
| `geog` | geography(Point, 4326) | no | Position. Distances on it are geodesic metres |
| `sog` | real | yes | Speed over ground, knots, 0-102.2. NULL: not available |
| `cog` | real | yes | Course over ground, degrees true, 0-359.9. NULL: not available |
| `heading` | smallint | yes | True heading, degrees, 0-359. NULL: no heading sensor (45% of real traffic) |
| `nav_status` | smallint | yes | AIS navigational status 0-15 (0 under way, 1 at anchor, 5 moored). NULL for class B |
| `msg_type` | smallint | no | AIS message type: 1, 2, 3 (class A), 18, 19 (class B), 27 (long range) |
| `source` | text | no | Provider name whose copy was stored first |
| `received_at` | timestamptz | no | When the collector (or the receiver, if the sentence carried a tag block) got it |
| `ingested_at` | timestamptz | no | When the row was inserted. `ingested_at - received_at` is pipeline latency |

Primary key `(mmsi, ts)`: the idempotency key. Indexes: GiST on `geog`, BRIN on `ts`.

## `vessel_latest`: the newest position of each vessel

Same fields as a position (`ts`, `sog`, `cog`, `heading`, `nav_status`), with `geom geometry(Point, 4326)`
and `updated_at` (when this row last changed, used by the map's delta polling). Primary key `mmsi`.
Only ever moves forward in `ts`.

## `coverage_cells`: empirical reception grid

| column | type | null | meaning |
|---|---|---|---|
| `cx`, `cy` | integer, PK | no | Cell index: the cell spans `cx * cell_deg` to `(cx + 1) * cell_deg` degrees of longitude, likewise `cy` in latitude |
| `cell_deg` | real | no | Cell size in degrees (default 0.25) |
| `hours_heard` | integer | no | Hours in the window in which at least one position was received from this cell |
| `hours_window` | integer | no | Hours in the window in which the system received anything anywhere |
| `vessels` | integer | no | Distinct vessels heard in the cell |
| `reliability` | real | no | `hours_heard / hours_window`, 0-1 |
| `geom` | geometry(Polygon, 4326) | no | The cell's outline |
| `computed_at` | timestamptz | no | |

Cells never heard do not exist; absence means reliability 0.

## `dark_events`: silences worth explaining

| column | type | null | meaning |
|---|---|---|---|
| `id` | bigint identity, PK | no | |
| `mmsi` | integer, FK `vessels` | no | |
| `started_at` | timestamptz | no | Last position before the silence |
| `ended_at` | timestamptz | yes | First position after it. NULL: still silent |
| `start_geog`, `end_geog` | geography(Point, 4326) | end: yes | Where it vanished and where it returned |
| `duration_s` | integer | no | Seconds of silence; for open events, up to the newest data held |
| `distance_m` | double precision | yes | Geodesic metres between the two positions. NULL while open |
| `implied_speed_kn` | real | yes | `distance / duration` in knots |
| `start_sog` | real | yes | Speed when it vanished, knots |
| `start_reliability` | real | no | Worst coverage reliability along 30 minutes of dead-reckoned track ahead of the last position |
| `end_reliability` | real | yes | The same, looking back from the reappearance |
| `neighbours_heard` | integer | no | Other vessels heard within 20 km of where it should have been, in the first 30 minutes |
| `classification` | text | no | `dark` (transponder off where we could hear), `coverage_gap` (we could not), `position_jump` (impossible hop with no real silence) |
| `impossible_speed` | boolean | no | Reappearance needed more than `GHOST_MAX_SPEED_KN` over at least 10 nm |
| `score` | real | no | Suspiciousness 0-100 (see `detect.assess`) |
| `reasons` | jsonb | no | Array of plain-language sentences justifying class and score |
| `detected_at` | timestamptz | no | First detection |

Unique `(mmsi, started_at)`. CHECKs keep an event either fully open or fully closed. Rows are deleted
when late data fills the gap. Index `(score DESC, id DESC)` serves the ranking.

## `quarantine`: inputs that were refused

| column | type | null | meaning |
|---|---|---|---|
| `cid` | text, PK part | no | Correlation id: `<source>/<archive file>:<byte offset>` of the raw line, or `kafka/<topic>/<partition>:<offset>` |
| `stage` | text, PK part | no | `decode` (framing or payload), `validate` (decoded but unacceptable), `sink` (refused at or by the database) |
| `reason` | text | no | Stable code: `bad_checksum`, `malformed_sentence`, `incomplete_multipart`, `orphan_fragment`, `non_vessel_mmsi`, `position_unavailable`, ... |
| `source` | text | no | Provider |
| `payload` | text | no | The original input, verbatim: raw NMEA line(s) for `decode`, the JSON message otherwise |
| `received_at` | timestamptz | no | |
| `quarantined_at` | timestamptz | no | |
| `replayed_at` | timestamptz | yes | Set by `ghostfleet replay-dlq` |

## `mid_countries`: reference

`mid` (smallint PK, 200-799), `iso2` (ISO 3166 alpha-2), `country`. Loaded from `ghostfleet/data/mid.csv` on every migrate.

## Files and topics

- **Raw archive** `data/raw/<source>/<YYYYMMDDTHH>.nmea`: one line per received line, `<unix receive time>\t<raw line>`. Append-only, never rewritten. `checkpoint.json` beside it records how far Kafka has acknowledged.
- **`ais.positions`**, **`ais.static`**: JSON, key MMSI. Fields as decoded, plus `v` (schema version), `source`, `received_at` (unix seconds), `cid`.
- **`ais.dlq`**: JSON `{cid, stage, reason, source, payload, received_at}`.
