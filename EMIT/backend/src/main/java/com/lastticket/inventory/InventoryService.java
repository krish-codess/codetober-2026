package com.lastticket.inventory;

import com.lastticket.api.ApiException;
import com.lastticket.inventory.Inventory.Change;
import com.lastticket.messaging.Outbox;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Function;
import java.util.function.Supplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * The write side. Every state change is: load aggregate, decide (pure), append the event at version + 1 and move the
 * snapshot with {@code WHERE version = ?}. A lost race rolls the whole transaction back and retries with backoff.
 */
@Service
public class InventoryService {
    private static final Logger log = LoggerFactory.getLogger(InventoryService.class);
    static final int MAX_ATTEMPTS = 20;
    /** Longest a request queues for its aggregate before being told to come back. */
    static final long GATE_WAIT_MS = 3000;

    /** Thrown inside a transaction when another writer moved the aggregate first. */
    static final class VersionConflict extends RuntimeException {
        VersionConflict() { super(null, null, false, false); }
    }

    public record NewInventory(String ticketType, String section, int priceCents, int total) {}

    public record NewEvent(UUID id, String name, Instant onSaleAt, int holdSeconds, int maxPerUser,
                           int admissionRatePerSec, List<NewInventory> inventory) {}

    private final JdbcClient db;
    private final TransactionTemplate tx;
    private final Outbox outbox;
    private final MeterRegistry metrics;
    private final Timer reserveTimer;
    private final int writersPerAggregate;
    private final Map<UUID, Semaphore> gates = new ConcurrentHashMap<>();
    private final Map<UUID, UUID> eventOfInventory = new ConcurrentHashMap<>();

    public InventoryService(JdbcClient db, TransactionTemplate tx, Outbox outbox, MeterRegistry metrics,
                            @Value("${lastticket.writers-per-aggregate:1}") int writersPerAggregate) {
        this.writersPerAggregate = writersPerAggregate;
        this.db = db;
        this.tx = tx;
        this.outbox = outbox;
        this.metrics = metrics;
        this.reserveTimer = Timer.builder("lastticket.reserve").publishPercentileHistogram().register(metrics);
    }

    // ---- reservations ------------------------------------------------------------------------------------------------

    /** Places a hold. Safe to retry: the same (user, idempotencyKey) always returns the same reservation. */
    public Reservation reserve(String userId, UUID inventoryId, int quantity, String idempotencyKey) {
        return reserveTimer.record(() -> {
            Optional<Reservation> replay = findByKey(userId, idempotencyKey);
            if (replay.isPresent()) {
                count("replayed");
                return replay.get();
            }
            UUID[] eventId = new UUID[1];
            try {
                // Fast refusal from a plain snapshot read, before queueing for the aggregate: once a section is gone,
                // the thousands still asking for it get their answer immediately and never contend with real buyers.
                Inventory snapshot = load(inventoryId);
                eventId[0] = snapshot.eventId();
                snapshot.placeHold(quantity);
                Reservation r = withRetry(inventoryId, () -> placeHold(userId, inventoryId, quantity, idempotencyKey, eventId));
                count("held");
                return r;
            } catch (DuplicateKeyException e) {
                // Either a concurrent retry with the same key won (return its result) or the user already has a basket.
                Optional<Reservation> winner = findByKey(userId, idempotencyKey);
                if (winner.isPresent()) {
                    count("replayed");
                    return winner.get();
                }
                throw rejected(eventId[0], inventoryId, quantity, new ApiException(HttpStatus.CONFLICT, "ALREADY_HOLDING",
                        "You already have tickets on hold for this event. Complete or release that order first."));
            } catch (ApiException e) {
                throw rejected(eventId[0], inventoryId, quantity, e);
            }
        });
    }

    private Reservation placeHold(String userId, UUID inventoryId, int quantity, String key, UUID[] eventIdOut) {
        // One round trip for everything the decision needs: the aggregate, the sale rules, and what this user already owns.
        Object[] row = db.sql("""
                SELECT i.id, i.event_id, i.ticket_type, i.section, i.price_cents, i.version, i.total, i.held, i.sold,
                       e.hold_seconds, e.max_per_user, now() >= e.on_sale_at AS open,
                       (SELECT coalesce(sum(r.quantity), 0) FROM reservation r
                         WHERE r.event_id = e.id AND r.user_id = ? AND r.status = 'CONFIRMED') AS owned
                  FROM inventory i JOIN sale_event e ON e.id = i.event_id WHERE i.id = ?""")
                .params(userId, inventoryId).query((rs, n) -> new Object[] {mapInventory(rs, n),
                        rs.getInt("hold_seconds"), rs.getInt("max_per_user"), rs.getBoolean("open"), rs.getInt("owned")})
                .optional().orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "INVENTORY_NOT_FOUND", "No such ticket section."));
        Inventory inv = (Inventory) row[0];
        UUID eventId = inv.eventId();
        int holdSeconds = (int) row[1], maxPerUser = (int) row[2], owned = (int) row[4];
        eventIdOut[0] = eventId;
        if (!(boolean) row[3]) {
            throw new ApiException(HttpStatus.CONFLICT, "NOT_ON_SALE", "This event is not on sale yet.");
        }
        if (owned + quantity > maxPerUser) {
            throw new ApiException(HttpStatus.CONFLICT, "LIMIT_EXCEEDED",
                    "Limit is " + maxPerUser + " tickets per person; you already have " + owned + ".");
        }
        UUID reservationId = UUID.randomUUID();
        append(inv, inv.placeHold(quantity), reservationId, userId, null);
        return db.sql("INSERT INTO reservation (id, inventory_id, event_id, user_id, quantity, status, idempotency_key, expires_at)"
                        + " VALUES (?, ?, ?, ?, ?, 'HELD', ?, now() + make_interval(secs => ?)) RETURNING " + Reservation.COLUMNS)
                .params(reservationId, inventoryId, eventId, userId, quantity, key, holdSeconds)
                .query(Reservation::map).single();
    }

    /** Turns a live hold into a sale. Idempotent; fails with 410 once the hold's deadline has passed. */
    public Reservation confirm(String userId, UUID reservationId) {
        return close(userId, reservationId, "CONFIRMED", "AND expires_at > now()", Inventory::confirmHold);
    }

    /** Gives the tickets back before the deadline (user abandoned checkout politely). Idempotent. */
    public Reservation release(String userId, UUID reservationId) {
        return close(userId, reservationId, "RELEASED", "", Inventory::releaseHold);
    }

    private Reservation close(String userId, UUID reservationId, String target, String guard,
                              java.util.function.BiFunction<Inventory, Integer, Change> decide) {
        UUID inventoryId = find(userId, reservationId).inventoryId(); // also: 404 before queueing for the aggregate
        return withRetry(inventoryId, () -> {
            // The status guard makes confirm / release / expire mutually exclusive: exactly one of them updates the row.
            Optional<Reservation> closed = db.sql("UPDATE reservation SET status = ?, closed_at = now() WHERE id = ? AND user_id = ?"
                            + " AND status = 'HELD' " + guard + " RETURNING " + Reservation.COLUMNS)
                    .params(target, reservationId, userId).query(Reservation::map).optional();
            if (closed.isEmpty()) {
                Reservation current = find(userId, reservationId);
                if (current.status().equals(target)) {
                    return current; // retry of a call that already succeeded
                }
                if (current.status().equals("HELD") || current.status().equals("EXPIRED")) {
                    throw new ApiException(HttpStatus.GONE, "HOLD_EXPIRED", "Your hold expired and the tickets went back on sale.");
                }
                throw new ApiException(HttpStatus.CONFLICT, "ALREADY_CLOSED", "This order is already " + current.status().toLowerCase() + ".");
            }
            Reservation r = closed.get();
            mutate(r.inventoryId(), inv -> decide.apply(inv, r.quantity()), r.id(), userId, null);
            return r;
        });
    }

    /** Called by the sweeper. Returns false if the hold was no longer due (confirmed, released or already swept). */
    public boolean expire(UUID reservationId, UUID inventoryId) {
        return withRetry(inventoryId, () -> {
            Optional<Reservation> due = db.sql("UPDATE reservation SET status = 'EXPIRED', closed_at = now() WHERE id = ?"
                            + " AND status = 'HELD' AND expires_at <= now() RETURNING " + Reservation.COLUMNS)
                    .param(reservationId).query(Reservation::map).optional();
            due.ifPresent(r -> mutate(r.inventoryId(), inv -> inv.expireHold(r.quantity()), r.id(), "sweeper", null));
            return due.isPresent();
        });
    }

    public Reservation find(String userId, UUID reservationId) {
        return db.sql("SELECT " + Reservation.COLUMNS + " FROM reservation WHERE id = ? AND user_id = ?")
                .params(reservationId, userId).query(Reservation::map).optional()
                // Someone else's reservation is indistinguishable from a missing one.
                .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "RESERVATION_NOT_FOUND", "No such reservation."));
    }

    /** Keyset page of a user's reservations, newest first. Cursor = (createdAt, id) of the last row seen. */
    public List<Reservation> list(String userId, Instant beforeCreatedAt, UUID beforeId, int limit) {
        if (beforeCreatedAt == null) {
            return db.sql("SELECT " + Reservation.COLUMNS + " FROM reservation WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?")
                    .params(userId, limit).query(Reservation::map).list();
        }
        return db.sql("SELECT " + Reservation.COLUMNS + " FROM reservation WHERE user_id = ? AND (created_at, id) < (?, ?)"
                        + " ORDER BY created_at DESC, id DESC LIMIT ?")
                .params(userId, beforeCreatedAt.atOffset(ZoneOffset.UTC), beforeId, limit).query(Reservation::map).list();
    }

    private Optional<Reservation> findByKey(String userId, String key) {
        return db.sql("SELECT " + Reservation.COLUMNS + " FROM reservation WHERE user_id = ? AND idempotency_key = ?")
                .params(userId, key).query(Reservation::map).optional();
    }

    // ---- stock -------------------------------------------------------------------------------------------------------

    /**
     * Stock release or correction. The caller states the version it saw; a stale or repeated request fails with 409
     * instead of being applied twice, which is what makes this safe to retry.
     */
    public Inventory adjustStock(UUID inventoryId, int delta, long expectedVersion, String reason) {
        try {
            return tx.execute(s -> mutate(inventoryId, inv -> {
                if (inv.version() != expectedVersion) {
                    throw new VersionConflict();
                }
                return inv.adjustStock(delta);
            }, null, "admin", reason));
        } catch (VersionConflict e) {
            throw new ApiException(HttpStatus.CONFLICT, "VERSION_MISMATCH",
                    "Inventory changed since version " + expectedVersion + " (now " + load(inventoryId).version() + "). Re-read and retry.");
        }
    }

    /** Creates an on-sale and its inventory. Idempotent on the event id: a repeat is a no-op. Returns false if it existed. */
    public boolean createEvent(NewEvent e) {
        return Boolean.TRUE.equals(tx.execute(s -> {
            int created = db.sql("INSERT INTO sale_event (id, name, on_sale_at, hold_seconds, max_per_user, admission_rate_per_sec)"
                            + " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING")
                    .params(e.id(), e.name(), e.onSaleAt().atOffset(ZoneOffset.UTC), e.holdSeconds(), e.maxPerUser(), e.admissionRatePerSec())
                    .update();
            if (created == 0) {
                return false;
            }
            for (NewInventory i : e.inventory()) {
                UUID id = UUID.randomUUID();
                db.sql("INSERT INTO inventory (id, event_id, ticket_type, section, price_cents) VALUES (?, ?, ?, ?, ?)")
                        .params(id, e.id(), i.ticketType(), i.section(), i.priceCents()).update();
                // Opening stock is an event like any other, so the stream alone reproduces the snapshot.
                mutate(id, inv -> inv.adjustStock(i.total()), null, "seed", "opening stock");
            }
            return true;
        }));
    }

    // ---- the core ----------------------------------------------------------------------------------------------------

    /** Which event a section belongs to. Immutable, so remembered: the hot path asks on every reservation. */
    public UUID eventOf(UUID inventoryId) {
        UUID known = eventOfInventory.get(inventoryId);
        if (known == null) {
            known = load(inventoryId).eventId();
            eventOfInventory.put(inventoryId, known);
        }
        return known;
    }

    Inventory load(UUID inventoryId) {
        return db.sql("SELECT id, event_id, ticket_type, section, price_cents, version, total, held, sold FROM inventory WHERE id = ?")
                .param(inventoryId).query(InventoryService::mapInventory).optional()
                .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "INVENTORY_NOT_FOUND", "No such ticket section."));
    }

    static Inventory mapInventory(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        return new Inventory(rs.getObject("id", UUID.class), rs.getObject("event_id", UUID.class), rs.getString("ticket_type"),
                rs.getString("section"), rs.getInt("price_cents"), rs.getLong("version"), rs.getInt("total"), rs.getInt("held"), rs.getInt("sold"));
    }

    /** Must run inside a transaction. Appends one event and moves the snapshot, or throws {@link VersionConflict}. */
    private Inventory mutate(UUID inventoryId, Function<Inventory, Change> decide, UUID reservationId, String actor, String reason) {
        Inventory inv = load(inventoryId);
        return append(inv, decide.apply(inv), reservationId, actor, reason);
    }

    /** The optimistic write: succeeds only if the aggregate is still at the version {@code inv} was read at. */
    private Inventory append(Inventory inv, Change c, UUID reservationId, String actor, String reason) {
        UUID inventoryId = inv.id();
        int moved = db.sql("UPDATE inventory SET total = total + ?, held = held + ?, sold = sold + ?, version = version + 1, updated_at = now()"
                        + " WHERE id = ? AND version = ?")
                .params(c.totalDelta(), c.heldDelta(), c.soldDelta(), inventoryId, inv.version()).update();
        if (moved == 0) {
            metrics.counter("lastticket.version.conflicts").increment();
            throw new VersionConflict();
        }
        // Inserted while this transaction holds the aggregate's row lock, so occurred_at is monotonic in version.
        db.sql("INSERT INTO inventory_event (inventory_id, version, type, total_delta, held_delta, sold_delta, reservation_id, reason, actor, correlation_id)"
                        + " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
                .params(inventoryId, inv.version() + 1, c.type().name(), c.totalDelta(), c.heldDelta(), c.soldDelta(),
                        reservationId, reason, actor, MDC.get("correlationId")).update();
        int quantity = Math.abs(c.totalDelta() != 0 ? c.totalDelta() : c.heldDelta());
        outbox.add(c.type().name(), inv.eventId(), inventoryId, reservationId, quantity, reason);
        return inv.apply(c);
    }

    /**
     * Runs {@code body} in a transaction, retrying lost version races with backoff.
     *
     * The gate lets one transaction per aggregate per instance into the database at a time; the rest wait in memory, in
     * arrival order, holding no connection. It is purely a contention limiter: without it every commit makes every
     * concurrent writer on that aggregate fail and retry (measured: 5.7 wasted attempts per hold, and a starved
     * connection pool). Correctness still rests on the version check alone, which is what arbitrates between instances.
     */
    private <T> T withRetry(UUID inventoryId, Supplier<T> body) {
        Semaphore gate = gates.computeIfAbsent(inventoryId, k -> new Semaphore(writersPerAggregate, true));
        try {
            if (!gate.tryAcquire(GATE_WAIT_MS, TimeUnit.MILLISECONDS)) {
                count("contention");
                throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "CONTENTION",
                        "Lots of people are buying right now. Your request was not processed; please try again.", 1);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "INTERRUPTED", "Server is shutting down; please retry.", 1);
        }
        try {
            return retrying(body);
        } finally {
            gate.release();
        }
    }

    private <T> T retrying(Supplier<T> body) {
        for (int attempt = 1; ; attempt++) {
            try {
                return tx.execute(s -> body.get());
            } catch (VersionConflict e) {
                if (attempt == MAX_ATTEMPTS) {
                    count("contention");
                    throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "CONTENTION",
                            "Lots of people are buying right now. Your request was not processed; please try again.", 1);
                }
                // Full-jitter exponential backoff, capped: 0..4 ms, 0..8 ms, 0..16 ms, then 0..32 ms.
                long capMs = Math.min(32, 2L << attempt);
                try {
                    Thread.sleep(ThreadLocalRandom.current().nextLong(capMs + 1));
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "INTERRUPTED", "Server is shutting down; please retry.", 1);
                }
            }
        }
    }

    /** Records a refused attempt for analytics (its own tiny transaction: the refused one rolled back). */
    private ApiException rejected(UUID eventId, UUID inventoryId, int quantity, ApiException e) {
        count(e.code().toLowerCase());
        if (eventId != null) {
            try {
                outbox.add("ATTEMPT_REJECTED", eventId, inventoryId, null, quantity, e.code());
            } catch (RuntimeException recordingFailure) {
                log.warn("could not record rejected attempt", recordingFailure);
            }
        }
        return e;
    }

    private void count(String outcome) {
        metrics.counter("lastticket.reserve.outcome", "outcome", outcome).increment();
    }
}
