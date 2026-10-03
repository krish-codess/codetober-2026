# Data dictionary

PostgreSQL 16. Defined in `backend/src/main/resources/db/migration`; rollback scripts in `db/rollback`.
All timestamps are `timestamptz` (UTC on the wire). Money is integer cents. "App may" is what the `lastticket_app` role is granted.

## sale_event — one on-sale. App may: SELECT, INSERT (rows are immutable)
| column | type | null | meaning |
|---|---|---|---|
| id | uuid | no | Primary key, supplied by the creator (makes creation idempotent). |
| name | text | no | 1–200 characters. |
| on_sale_at | timestamptz | no | The hard start. Before it, reservations are refused and the waiting room is a lobby. |
| hold_seconds | int | no | How long a hold lasts. 5–3600 s. |
| max_per_user | int | no | Ticket limit per person per event (confirmed + being held). 1–10. |
| admission_rate_per_sec | int | no | People the waiting room admits per second. 1–10000. |
| created_at | timestamptz | no | |

## inventory — the aggregate and its snapshot. App may: SELECT, INSERT, UPDATE
One row per (event, ticket type, section). `available = total - held - sold` is the available-to-promise figure.
| column | type | null | meaning |
|---|---|---|---|
| id | uuid | no | Primary key. |
| event_id | uuid | no | FK → sale_event. |
| ticket_type, section | text | no | Upper-case codes, `[A-Z0-9_]{1,32}`. Unique with event_id. |
| price_cents | int | no | Price per ticket, cents, ≥ 0. |
| version | bigint | no | Number of events applied. The optimistic concurrency token. |
| total | int | no | Tickets that exist. ≥ 0. |
| held | int | no | Tickets in unexpired-or-not-yet-swept holds. ≥ 0. |
| sold | int | no | Tickets in confirmed reservations. ≥ 0. |
| updated_at | timestamptz | no | |

Constraints: `inventory_no_oversell CHECK (held + sold <= total)`; `inventory_natural_key UNIQUE (event_id, ticket_type, section)`.

## inventory_event — append-only event stream. App may: SELECT, INSERT
| column | type | null | meaning |
|---|---|---|---|
| seq | bigserial | no | Primary key; global insertion order (pagination cursor). |
| inventory_id | uuid | no | FK → inventory. |
| version | bigint | no | Position in this aggregate's stream, from 1. `UNIQUE (inventory_id, version)`. |
| type | text | no | `STOCK_ADDED`, `STOCK_CORRECTED`, `HOLD_PLACED`, `HOLD_CONFIRMED`, `HOLD_EXPIRED`, `HOLD_RELEASED`. |
| total_delta, held_delta, sold_delta | int | no | Effect on the snapshot, in tickets. Position at time T = sum of deltas with `occurred_at <= T`. |
| reservation_id | uuid | yes | Set for `HOLD_*` events, NULL for stock events (CHECK). |
| reason | text | yes | Free text on stock changes. |
| actor | text | no | User id, `admin`, `sweeper` or `seed`. |
| correlation_id | text | yes | `X-Request-Id` of the request that caused it; NULL for background work. |
| occurred_at | timestamptz | no | Database clock, taken while the aggregate's row lock is held, so monotonic in version. |

## reservation — a hold and what became of it. App may: SELECT, INSERT, UPDATE
| column | type | null | meaning |
|---|---|---|---|
| id | uuid | no | Primary key. |
| inventory_id, event_id | uuid | no | FKs. |
| user_id | text | no | Session subject (or the feed's user id for ingested attempts). |
| quantity | int | no | 1–10 tickets. |
| status | text | no | `HELD` → `CONFIRMED` \| `EXPIRED` \| `RELEASED`. Never leaves a terminal state. |
| idempotency_key | text | no | Client-supplied. `UNIQUE (user_id, idempotency_key)`. |
| expires_at | timestamptz | no | Hard deadline for confirming. |
| created_at | timestamptz | no | |
| closed_at | timestamptz | yes | When it left `HELD`; NULL exactly while `HELD` (CHECK). |

Indexes: `reservation_one_active_hold UNIQUE (event_id, user_id) WHERE status = 'HELD'`;
`reservation_due (expires_at) WHERE status = 'HELD'`; `reservation_by_user (user_id, created_at DESC, id DESC)`.

## outbox — messages awaiting Kafka. App may: SELECT, INSERT, UPDATE
| column | type | null | meaning |
|---|---|---|---|
| id | bigserial | no | Publish order. |
| topic, msg_key | text | no | Destination topic; key = inventory id. |
| payload | text | no | JSON: `{id, type, eventId, inventoryId, reservationId, quantity, reason, occurredAt}`. `id` is the dedup key. |
| created_at | timestamptz | no | |
| published_at | timestamptz | yes | NULL until the broker acknowledged it. Index `outbox_unpublished (id) WHERE published_at IS NULL`. |

## processed_message — consumer idempotency. App may: SELECT, INSERT
`(consumer text, message_id uuid)` primary key, `processed_at`. A row means that consumer has applied that message.

## dead_letter — messages a consumer gave up on. App may: SELECT, INSERT, UPDATE
`id`, `topic`, `kafka_partition`, `kafka_offset` (unique together), `msg_key` (nullable), `payload` (original, nullable),
`reason` (exception class and message), `failed_at`, `replayed_at` (NULL until replayed).

## sale_stat — per-second counters, built from Kafka. App may: SELECT, INSERT, UPDATE
`(event_id, bucket, metric)` primary key; `bucket` is the start of a 1-second window; `n` ≥ 0.
Metrics: `attempts`, `holds`, `rejected_sold_out` (the oversell attempts), `rejected_other`, `confirmed`, `expired`, `released`.

## raw_attempt — every ingested feed line, verbatim. App may: SELECT, INSERT
`id`, `batch_id` + `line_no` (unique; makes re-sending a batch a no-op), `payload` (the line exactly as received), `received_at`.

## attempt_outcome — what happened to each raw line. App may: SELECT, INSERT
| column | type | null | meaning |
|---|---|---|---|
| raw_attempt_id | bigint | no | PK and FK → raw_attempt. |
| status | text | no | `HELD`, `REJECTED` (valid but refused, e.g. sold out) or `QUARANTINED` (invalid). |
| reason | text | yes | Machine-readable code; NULL exactly when `HELD` (CHECK). |
| reservation_id | uuid | yes | The resulting reservation when `HELD`. |
| client_ts | timestamptz | yes | The feed's own timestamp when parseable. Informational only. |
| processed_at | timestamptz | no | |

## Redis keys
`{eventId}` is a hash tag: all of an event's keys land on one cluster slot, so the Lua scripts stay valid on Redis Cluster.
Eviction policy is `noeviction`: under memory pressure writes fail loudly rather than silently dropping queue members.

| key | type | TTL | written by | if lost |
|---|---|---|---|---|
| `wr:{eventId}:q` | ZSET user → score | 24 h, refreshed on join | join (Lua, `ZADD NX`) | users get `NOT_IN_QUEUE` and rejoin |
| `wr:{eventId}:adm:{userId}` | string = expiry epoch ms | `ADMISSION_TTL_SECONDS` (600) | admit (Lua) | already-issued tokens still verify; others rejoin |
| `wr:{eventId}:tick:{epochSecond}` | string | 5 s | admit (Lua, `SET NX`) | at worst one extra admission batch |
| `atp:{eventId}` | string (JSON availability) | 1 s | availability read on miss | next read goes to Postgres |

## Kafka
| topic | partitions | key | retention | producer | consumer group |
|---|---|---|---|---|---|
| `ticketing.events` | 6 | inventory id (per-aggregate order) | 7 days | outbox relay, `acks=all`, idempotent producer | `last-ticket-stats` (offsets committed only after the listener returns) |

Delivery: at-least-once. Consumers are idempotent on `payload.id`. Failures: 3 retries with backoff, then `dead_letter`.
