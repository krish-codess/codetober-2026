package com.lastticket.intake;

import com.lastticket.api.ApiException;
import com.lastticket.intake.AttemptParser.Attempt;
import com.lastticket.inventory.InventoryService;
import com.lastticket.inventory.Reservation;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.TreeMap;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

/**
 * Batch intake of the purchase-attempt feed (NDJSON). Every line is stored verbatim first (raw_attempt, append-only),
 * then validated; failures are quarantined with a reason, never dropped. Valid attempts go through exactly the same
 * {@link InventoryService#reserve} as live HTTP traffic, with attempt_id as the idempotency key, so duplicate lines and
 * re-sent batches cannot double-book.
 */
@Service
public class AttemptIntake {

    public record Summary(UUID batchId, int lines, int held, int rejected, int quarantined, int alreadyProcessed,
                          Map<String, Integer> reasons) {}

    public record Quarantined(long id, UUID batchId, int lineNo, String reason, String payload, Instant receivedAt) {}

    private final JdbcClient db;
    private final InventoryService inventory;

    public AttemptIntake(JdbcClient db, InventoryService inventory) {
        this.db = db;
        this.inventory = inventory;
    }

    // ponytail: synchronous and sequential, fine for files of ~10^4 lines; hand batches to a worker pool (or produce
    // them to Kafka) if feeds get big enough for the HTTP request to time out.
    public Summary ingest(UUID batchId, String body) {
        String[] lines = body.split("\r?\n");
        int held = 0, rejected = 0, quarantined = 0, already = 0, seen = 0;
        Map<String, Integer> reasons = new TreeMap<>();
        Map<String, Optional<UUID>> inventoryIds = new HashMap<>();
        for (int i = 0; i < lines.length; i++) {
            String line = lines[i];
            if (line.isBlank()) {
                continue;
            }
            seen++;
            Optional<Long> rawId = db.sql("INSERT INTO raw_attempt (batch_id, line_no, payload) VALUES (?, ?, ?)"
                    + " ON CONFLICT (batch_id, line_no) DO NOTHING RETURNING id").params(batchId, i + 1, line).query(Long.class).optional();
            if (rawId.isEmpty()) {
                // The batch was sent before. Only lines that never got an outcome (crash mid-batch) are processed now.
                rawId = db.sql("SELECT r.id FROM raw_attempt r WHERE r.batch_id = ? AND r.line_no = ?"
                                + " AND NOT EXISTS (SELECT 1 FROM attempt_outcome o WHERE o.raw_attempt_id = r.id)")
                        .params(batchId, i + 1).query(Long.class).optional();
                if (rawId.isEmpty()) {
                    already++;
                    continue;
                }
            }
            String reason;
            Attempt a = null;
            try {
                a = AttemptParser.parse(line);
                Attempt parsed = a;
                Optional<UUID> inventoryId = inventoryIds.computeIfAbsent(a.eventId() + "/" + a.ticketType() + "/" + a.section(),
                        k -> db.sql("SELECT id FROM inventory WHERE event_id = ? AND ticket_type = ? AND section = ?")
                                .params(parsed.eventId(), parsed.ticketType(), parsed.section()).query(UUID.class).optional());
                if (inventoryId.isEmpty()) {
                    throw new AttemptParser.Invalid("UNKNOWN_INVENTORY");
                }
                Reservation r = inventory.reserve(a.userId(), inventoryId.get(), a.quantity(), a.attemptId());
                outcome(rawId.get(), "HELD", null, r.id(), a.clientTs());
                held++;
                continue;
            } catch (AttemptParser.Invalid e) {
                reason = e.getMessage();
                outcome(rawId.get(), "QUARANTINED", reason, null, a == null ? null : a.clientTs());
                quarantined++;
            } catch (ApiException e) {
                reason = e.code();
                outcome(rawId.get(), "REJECTED", reason, null, a.clientTs());
                rejected++;
            }
            reasons.merge(reason, 1, Integer::sum);
        }
        return new Summary(batchId, seen, held, rejected, quarantined, already, reasons);
    }

    /** Keyset page of quarantined lines, oldest first, with the original payload. */
    public List<Quarantined> quarantine(long afterId, int limit) {
        return db.sql("SELECT r.id, r.batch_id, r.line_no, o.reason, r.payload, r.received_at FROM attempt_outcome o"
                        + " JOIN raw_attempt r ON r.id = o.raw_attempt_id WHERE o.status = 'QUARANTINED' AND o.raw_attempt_id > ?"
                        + " ORDER BY o.raw_attempt_id LIMIT ?")
                .params(afterId, limit)
                .query((rs, n) -> new Quarantined(rs.getLong("id"), rs.getObject("batch_id", UUID.class), rs.getInt("line_no"),
                        rs.getString("reason"), rs.getString("payload"), rs.getObject("received_at", java.time.OffsetDateTime.class).toInstant()))
                .list();
    }

    private void outcome(long rawId, String status, String reason, UUID reservationId, Instant clientTs) {
        db.sql("INSERT INTO attempt_outcome (raw_attempt_id, status, reason, reservation_id, client_ts) VALUES (?, ?, ?, ?, ?)"
                        + " ON CONFLICT (raw_attempt_id) DO NOTHING")
                .params(rawId, status, reason, reservationId, clientTs == null ? null : clientTs.atOffset(ZoneOffset.UTC)).update();
    }
}
