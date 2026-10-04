-- ---------------------------------------------------------------------------------------------------------------------
-- sale_event: one on-sale with a hard start time.
-- ---------------------------------------------------------------------------------------------------------------------
CREATE TABLE sale_event (
  id                     uuid        PRIMARY KEY,
  name                   text        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  on_sale_at             timestamptz NOT NULL,
  hold_seconds           int         NOT NULL CHECK (hold_seconds BETWEEN 5 AND 3600),
  max_per_user           int         NOT NULL CHECK (max_per_user BETWEEN 1 AND 10),
  admission_rate_per_sec int         NOT NULL CHECK (admission_rate_per_sec BETWEEN 1 AND 10000),
  created_at             timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------------------------------
-- inventory: the aggregate, one per (event, ticket type, section). This row is the current snapshot AND the
-- available-to-promise projection (available = total - held - sold); inventory_event is the source of truth.
-- `version` is the optimistic concurrency token: every write is  UPDATE ... WHERE id = ? AND version = ?.
-- The CHECK is the last line of defence against oversell: no code path can commit held + sold > total.
-- ---------------------------------------------------------------------------------------------------------------------
CREATE TABLE inventory (
  id          uuid        PRIMARY KEY,
  event_id    uuid        NOT NULL REFERENCES sale_event (id),
  ticket_type text        NOT NULL CHECK (ticket_type ~ '^[A-Z0-9_]{1,32}$'),
  section     text        NOT NULL CHECK (section ~ '^[A-Z0-9_]{1,32}$'),
  price_cents int         NOT NULL CHECK (price_cents >= 0),
  version     bigint      NOT NULL DEFAULT 0 CHECK (version >= 0),
  total       int         NOT NULL DEFAULT 0 CHECK (total >= 0),
  held        int         NOT NULL DEFAULT 0 CHECK (held >= 0),
  sold        int         NOT NULL DEFAULT 0 CHECK (sold >= 0),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT inventory_natural_key UNIQUE (event_id, ticket_type, section),
  CONSTRAINT inventory_no_oversell CHECK (held + sold <= total)
);

-- ---------------------------------------------------------------------------------------------------------------------
-- inventory_event: append-only event stream per aggregate. UNIQUE (inventory_id, version) is the optimistic lock at the
-- storage level: two writers that both read version N cannot both append N+1.
-- Deltas are stored as columns so the position at any instant is a SUM over a range, not a replay in application code.
-- ---------------------------------------------------------------------------------------------------------------------
CREATE TABLE inventory_event (
  seq            bigserial   PRIMARY KEY,
  inventory_id   uuid        NOT NULL REFERENCES inventory (id),
  version        bigint      NOT NULL CHECK (version > 0),
  type           text        NOT NULL CHECK (type IN
                   ('STOCK_ADDED', 'STOCK_CORRECTED', 'HOLD_PLACED', 'HOLD_CONFIRMED', 'HOLD_EXPIRED', 'HOLD_RELEASED')),
  total_delta    int         NOT NULL DEFAULT 0,
  held_delta     int         NOT NULL DEFAULT 0,
  sold_delta     int         NOT NULL DEFAULT 0,
  reservation_id uuid,                 -- NULL for stock events
  reason         text,                 -- free text for corrections; NULL otherwise
  actor          text        NOT NULL, -- user id, 'admin', 'sweeper', 'seed'
  correlation_id text,                 -- request id that caused the event, when there was one
  occurred_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT inventory_event_version UNIQUE (inventory_id, version),
  CONSTRAINT inventory_event_hold_has_reservation CHECK ((type LIKE 'HOLD_%') = (reservation_id IS NOT NULL))
);
-- Serves point-in-time reconstruction: SUM(deltas) WHERE inventory_id = ? AND occurred_at <= ?  (docs/explain/as-of.txt)
CREATE INDEX inventory_event_as_of ON inventory_event (inventory_id, occurred_at) INCLUDE (total_delta, held_delta, sold_delta);

-- ---------------------------------------------------------------------------------------------------------------------
-- reservation: a hold on N tickets with a hard expiry.
-- ---------------------------------------------------------------------------------------------------------------------
CREATE TABLE reservation (
  id              uuid        PRIMARY KEY,
  inventory_id    uuid        NOT NULL REFERENCES inventory (id),
  event_id        uuid        NOT NULL REFERENCES sale_event (id),
  user_id         text        NOT NULL CHECK (length(user_id) BETWEEN 1 AND 64),
  quantity        int         NOT NULL CHECK (quantity BETWEEN 1 AND 10),
  status          text        NOT NULL CHECK (status IN ('HELD', 'CONFIRMED', 'EXPIRED', 'RELEASED')),
  idempotency_key text        NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 100),
  expires_at      timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  closed_at       timestamptz,          -- when it left HELD; NULL while HELD
  CONSTRAINT reservation_idempotency UNIQUE (user_id, idempotency_key),
  CONSTRAINT reservation_closed_iff_not_held CHECK ((status = 'HELD') = (closed_at IS NULL)),
  CONSTRAINT reservation_expiry_after_creation CHECK (expires_at > created_at)
);
-- A user can hold at most one basket per event at a time (stops one admitted user hoarding stock with parallel holds).
CREATE UNIQUE INDEX reservation_one_active_hold ON reservation (event_id, user_id) WHERE status = 'HELD';
-- Serves the expiry sweeper: WHERE status = 'HELD' AND expires_at <= now() ORDER BY expires_at  (docs/explain/sweeper.txt)
CREATE INDEX reservation_due ON reservation (expires_at) WHERE status = 'HELD';
-- Serves "my reservations" keyset pagination: WHERE user_id = ? AND (created_at, id) < (?, ?) ORDER BY created_at DESC, id DESC
CREATE INDEX reservation_by_user ON reservation (user_id, created_at DESC, id DESC);

-- ---------------------------------------------------------------------------------------------------------------------
-- outbox: messages for Kafka, written in the same transaction as the state change they describe.
-- ---------------------------------------------------------------------------------------------------------------------
CREATE TABLE outbox (
  id           bigserial   PRIMARY KEY,
  topic        text        NOT NULL,
  msg_key      text        NOT NULL,
  payload      text        NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);
-- Serves the publisher: WHERE published_at IS NULL ORDER BY id LIMIT n  (docs/explain/outbox.txt)
CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

-- Consumer-side idempotency: a message id is processed at most once per consumer.
CREATE TABLE processed_message (
  consumer     text        NOT NULL,
  message_id   uuid        NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, message_id)
);

-- Messages a consumer gave up on, with the original payload and the reason. Replayable.
CREATE TABLE dead_letter (
  id           bigserial   PRIMARY KEY,
  topic        text        NOT NULL,
  kafka_partition int      NOT NULL,
  kafka_offset bigint      NOT NULL,
  msg_key      text,
  payload      text,
  reason       text        NOT NULL,
  failed_at    timestamptz NOT NULL DEFAULT now(),
  replayed_at  timestamptz,
  CONSTRAINT dead_letter_position UNIQUE (topic, kafka_partition, kafka_offset)
);

-- ---------------------------------------------------------------------------------------------------------------------
-- sale_stat: per-second counters built by the Kafka consumer (analytics projection).
-- metric: attempts | holds | rejected_sold_out | rejected_other | confirmed | expired | released
-- ---------------------------------------------------------------------------------------------------------------------
CREATE TABLE sale_stat (
  event_id uuid        NOT NULL REFERENCES sale_event (id),
  bucket   timestamptz NOT NULL, -- start of the 1-second bucket, UTC
  metric   text        NOT NULL,
  n        bigint      NOT NULL CHECK (n >= 0),
  PRIMARY KEY (event_id, bucket, metric)
);

-- ---------------------------------------------------------------------------------------------------------------------
-- Batch intake. raw_attempt keeps every received line byte-for-byte; attempt_outcome records what we did with it.
-- ---------------------------------------------------------------------------------------------------------------------
CREATE TABLE raw_attempt (
  id          bigserial   PRIMARY KEY,
  batch_id    uuid        NOT NULL,
  line_no     int         NOT NULL CHECK (line_no > 0),
  payload     text        NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT raw_attempt_line UNIQUE (batch_id, line_no)
);
CREATE TABLE attempt_outcome (
  raw_attempt_id bigint      PRIMARY KEY REFERENCES raw_attempt (id),
  status         text        NOT NULL CHECK (status IN ('HELD', 'REJECTED', 'QUARANTINED')),
  reason         text,        -- machine-readable code; required unless HELD
  reservation_id uuid        REFERENCES reservation (id),
  client_ts      timestamptz, -- normalised client timestamp when parseable (informational: server time decides)
  processed_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT attempt_outcome_reason CHECK ((status = 'HELD') = (reason IS NULL))
);
-- Serves the quarantine review list: WHERE status = 'QUARANTINED' AND raw_attempt_id > ? ORDER BY raw_attempt_id
CREATE INDEX attempt_outcome_quarantined ON attempt_outcome (raw_attempt_id) WHERE status = 'QUARANTINED';

-- ---------------------------------------------------------------------------------------------------------------------
-- Grants. Append-only tables get INSERT + SELECT only: immutability is enforced by privilege, not by convention.
-- ---------------------------------------------------------------------------------------------------------------------
GRANT SELECT, INSERT ON sale_event TO ${app_user};
GRANT SELECT, INSERT, UPDATE ON inventory, reservation, outbox, dead_letter, sale_stat TO ${app_user};
GRANT SELECT, INSERT ON inventory_event, raw_attempt, attempt_outcome, processed_message TO ${app_user};
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ${app_user};
