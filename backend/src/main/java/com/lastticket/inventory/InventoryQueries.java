package com.lastticket.inventory;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.lastticket.api.ApiException;
import io.swagger.v3.oas.annotations.media.Schema;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

/** The read side. Nothing here takes a lock: reads are plain MVCC snapshots, with a short Redis cache in front. */
@Service
public class InventoryQueries {
    private static final Logger log = LoggerFactory.getLogger(InventoryQueries.class);
    static final Duration ATP_TTL = Duration.ofSeconds(1);

    public record EventView(UUID id, String name, Instant onSaleAt, int holdSeconds, int maxPerUser,
                            int admissionRatePerSec, Instant serverTime) {}

    public record Section(UUID inventoryId, String ticketType, String section, int priceCents, int available, int total) {}

    public record Availability(UUID eventId,
                               @Schema(description = "When this snapshot was read from the database. Served from cache for up to 1 s.")
                               Instant asOf,
                               List<Section> sections) {}

    public record Position(UUID inventoryId, String ticketType, String section, long version, int total, int held, int sold, int available) {}

    public record StreamEvent(long seq, long version, String type, int totalDelta, int heldDelta, int soldDelta,
                              UUID reservationId, String reason, String actor, String correlationId, Instant occurredAt) {}

    public record Analytics(
            long attempts, long holds,
            @Schema(description = "Attempts refused because granting them would have exceeded stock.") long oversellAttempts,
            @Schema(description = "oversellAttempts / attempts") double oversellAttemptRate,
            long otherRejections, long confirmed, long expired, long released,
            @Schema(description = "confirmed / holds") double holdConversionRate,
            @Schema(description = "expired / holds") double holdExpiryRate,
            @Schema(description = "Tickets committed beyond stock, summed over sections, read from the live snapshot. Must be 0.") int oversoldTickets,
            @Schema(description = "Sections whose snapshot disagrees with a replay of their event stream. Must be 0.") int snapshotDrift) {}

    private final JdbcClient db;
    private final StringRedisTemplate redis;
    private final ObjectMapper json;
    private final Map<UUID, EventView> events = new java.util.concurrent.ConcurrentHashMap<>();

    public InventoryQueries(JdbcClient db, StringRedisTemplate redis, ObjectMapper json) {
        this.db = db;
        this.redis = redis;
        this.json = json;
    }

    /**
     * sale_event rows are immutable (the app role cannot even UPDATE them), so once read they are served from memory:
     * waiting-room joins and polls, the busiest calls of an on-sale, never touch Postgres. serverTime is then this
     * instance's clock; hold deadlines are always judged by the database's.
     */
    public EventView event(UUID eventId) {
        EventView e = events.get(eventId);
        if (e == null) {
            e = db.sql("SELECT id, name, on_sale_at, hold_seconds, max_per_user, admission_rate_per_sec, now() AS server_time FROM sale_event WHERE id = ?")
                    .param(eventId).query(InventoryQueries::mapEvent).optional()
                    .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "EVENT_NOT_FOUND", "No such event."));
            events.put(eventId, e);
        }
        return new EventView(e.id(), e.name(), e.onSaleAt(), e.holdSeconds(), e.maxPerUser(), e.admissionRatePerSec(), Instant.now());
    }

    /** Keyset page ordered by (on_sale_at, id). */
    public List<EventView> events(Instant afterOnSaleAt, UUID afterId, int limit) {
        String select = "SELECT id, name, on_sale_at, hold_seconds, max_per_user, admission_rate_per_sec, now() AS server_time FROM sale_event ";
        if (afterOnSaleAt == null) {
            return db.sql(select + "ORDER BY on_sale_at, id LIMIT ?").param(limit).query(InventoryQueries::mapEvent).list();
        }
        return db.sql(select + "WHERE (on_sale_at, id) > (?, ?) ORDER BY on_sale_at, id LIMIT ?")
                .params(afterOnSaleAt.atOffset(ZoneOffset.UTC), afterId, limit).query(InventoryQueries::mapEvent).list();
    }

    /** Events whose sale has started within the last day: the ones the waiting room must keep admitting for. */
    public List<EventView> onSaleNow() {
        return db.sql("SELECT id, name, on_sale_at, hold_seconds, max_per_user, admission_rate_per_sec, now() AS server_time FROM sale_event"
                + " WHERE on_sale_at <= now() AND on_sale_at > now() - interval '1 day'").query(InventoryQueries::mapEvent).list();
    }

    /**
     * Available-to-promise. Redis first; on a miss, or if Redis is down, the snapshot rows in Postgres (an index scan
     * of a handful of rows that never waits on writers).
     */
    public Availability availability(UUID eventId) {
        String key = "atp:{" + eventId + "}";
        try {
            String cached = redis.opsForValue().get(key);
            if (cached != null) {
                return json.readValue(cached, Availability.class);
            }
        } catch (RuntimeException | JsonProcessingException e) {
            log.warn("availability cache read failed, serving from database: {}", e.toString());
        }
        // ponytail: no stampede protection; on expiry concurrent readers each run this small query once. Add a
        // single-flight lock if the query ever stops being a 5-row index scan.
        List<Section> sections = db.sql("SELECT id, ticket_type, section, price_cents, total - held - sold AS available, total"
                        + " FROM inventory WHERE event_id = ? ORDER BY ticket_type, section")
                .param(eventId).query((rs, n) -> new Section(rs.getObject("id", UUID.class), rs.getString("ticket_type"),
                        rs.getString("section"), rs.getInt("price_cents"), rs.getInt("available"), rs.getInt("total"))).list();
        if (sections.isEmpty()) {
            event(eventId); // 404 if the event itself is unknown
        }
        Availability fresh = new Availability(eventId, Instant.now(), sections);
        try {
            redis.opsForValue().set(key, json.writeValueAsString(fresh), ATP_TTL);
        } catch (RuntimeException | JsonProcessingException e) {
            log.warn("availability cache write failed: {}", e.toString());
        }
        return fresh;
    }

    /** The exact position of every section as of {@code at}, rebuilt from the event stream alone. */
    public List<Position> positionAsOf(UUID eventId, Instant at) {
        event(eventId);
        return db.sql("""
                SELECT i.id, i.ticket_type, i.section, coalesce(max(e.version), 0) AS version,
                       coalesce(sum(e.total_delta), 0) AS total, coalesce(sum(e.held_delta), 0) AS held, coalesce(sum(e.sold_delta), 0) AS sold
                  FROM inventory i LEFT JOIN inventory_event e ON e.inventory_id = i.id AND e.occurred_at <= ?
                 WHERE i.event_id = ? GROUP BY i.id ORDER BY i.ticket_type, i.section""")
                .params(at.atOffset(ZoneOffset.UTC), eventId)
                .query((rs, n) -> new Position(rs.getObject("id", UUID.class), rs.getString("ticket_type"), rs.getString("section"),
                        rs.getLong("version"), rs.getInt("total"), rs.getInt("held"), rs.getInt("sold"),
                        rs.getInt("total") - rs.getInt("held") - rs.getInt("sold"))).list();
    }

    /** Keyset page of one aggregate's event stream, oldest first. */
    public List<StreamEvent> stream(UUID inventoryId, long afterSeq, int limit) {
        return db.sql("SELECT seq, version, type, total_delta, held_delta, sold_delta, reservation_id, reason, actor, correlation_id, occurred_at"
                        + " FROM inventory_event WHERE inventory_id = ? AND seq > ? ORDER BY seq LIMIT ?")
                .params(inventoryId, afterSeq, limit)
                .query((rs, n) -> new StreamEvent(rs.getLong("seq"), rs.getLong("version"), rs.getString("type"), rs.getInt("total_delta"),
                        rs.getInt("held_delta"), rs.getInt("sold_delta"), rs.getObject("reservation_id", UUID.class), rs.getString("reason"),
                        rs.getString("actor"), rs.getString("correlation_id"), Reservation.instant(rs, "occurred_at"))).list();
    }

    public Analytics analytics(UUID eventId) {
        event(eventId);
        Map<String, Long> m = new LinkedHashMap<>();
        db.sql("SELECT metric, sum(n) AS n FROM sale_stat WHERE event_id = ? GROUP BY metric").param(eventId)
                .query(rs -> { m.put(rs.getString("metric"), rs.getLong("n")); });
        long attempts = m.getOrDefault("attempts", 0L), holds = m.getOrDefault("holds", 0L), oversell = m.getOrDefault("rejected_sold_out", 0L);
        int oversold = db.sql("SELECT coalesce(sum(greatest(held + sold - total, 0)), 0) FROM inventory WHERE event_id = ?")
                .param(eventId).query(Integer.class).single();
        int drift = db.sql("""
                SELECT count(*) FROM inventory i
                  LEFT JOIN LATERAL (SELECT coalesce(max(e.version), 0) AS version, coalesce(sum(e.total_delta), 0) AS total,
                                            coalesce(sum(e.held_delta), 0) AS held, coalesce(sum(e.sold_delta), 0) AS sold
                                       FROM inventory_event e WHERE e.inventory_id = i.id) replay ON true
                 WHERE i.event_id = ?
                   AND (i.version <> replay.version OR i.total <> replay.total OR i.held <> replay.held OR i.sold <> replay.sold)""")
                .param(eventId).query(Integer.class).single();
        return new Analytics(attempts, holds, oversell, ratio(oversell, attempts), m.getOrDefault("rejected_other", 0L),
                m.getOrDefault("confirmed", 0L), m.getOrDefault("expired", 0L), m.getOrDefault("released", 0L),
                ratio(m.getOrDefault("confirmed", 0L), holds), ratio(m.getOrDefault("expired", 0L), holds), oversold, drift);
    }

    private static double ratio(long a, long b) {
        return b == 0 ? 0 : Math.round(1e4 * a / b) / 1e4;
    }

    private static EventView mapEvent(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        return new EventView(rs.getObject("id", UUID.class), rs.getString("name"), Reservation.instant(rs, "on_sale_at"),
                rs.getInt("hold_seconds"), rs.getInt("max_per_user"), rs.getInt("admission_rate_per_sec"), Reservation.instant(rs, "server_time"));
    }
}
