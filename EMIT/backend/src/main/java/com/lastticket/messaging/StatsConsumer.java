package com.lastticket.messaging;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.List;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Downstream consumer: folds the event stream into per-second counters (sale_stat).
 * Delivery is at-least-once; the processed_message row and the counter updates commit in one transaction, so a
 * redelivered message changes nothing. Net effect: effectively-once.
 */
@Component
public class StatsConsumer {
    static final String CONSUMER = "stats";

    private final JdbcClient db;
    private final TransactionTemplate tx;
    private final ObjectMapper json;

    public StatsConsumer(JdbcClient db, TransactionTemplate tx, ObjectMapper json) {
        this.db = db;
        this.tx = tx;
        this.json = json;
    }

    @KafkaListener(topics = Outbox.TOPIC)
    public void onMessage(String payload) {
        Outbox.Message msg;
        try {
            msg = json.readValue(payload, Outbox.Message.class);
        } catch (JsonProcessingException e) {
            throw new PoisonMessageException("unparseable payload: " + e.getOriginalMessage());
        }
        if (msg == null || msg.id() == null || msg.type() == null || msg.eventId() == null || msg.occurredAt() == null) {
            throw new PoisonMessageException("missing id, type, eventId or occurredAt");
        }
        List<String> stats = switch (msg.type()) {
            case "HOLD_PLACED" -> List.of("attempts", "holds");
            case "ATTEMPT_REJECTED" -> List.of("attempts", "SOLD_OUT".equals(msg.reason()) ? "rejected_sold_out" : "rejected_other");
            case "HOLD_CONFIRMED" -> List.of("confirmed");
            case "HOLD_EXPIRED" -> List.of("expired");
            case "HOLD_RELEASED" -> List.of("released");
            case "STOCK_ADDED", "STOCK_CORRECTED" -> List.of();
            default -> throw new PoisonMessageException("unknown type " + msg.type());
        };
        tx.executeWithoutResult(s -> {
            int first = db.sql("INSERT INTO processed_message (consumer, message_id) VALUES (?, ?) ON CONFLICT DO NOTHING")
                    .params(CONSUMER, msg.id()).update();
            if (first == 0) {
                return; // duplicate delivery
            }
            for (String metric : stats) {
                db.sql("INSERT INTO sale_stat (event_id, bucket, metric, n) VALUES (?, ?, ?, 1)"
                                + " ON CONFLICT (event_id, bucket, metric) DO UPDATE SET n = sale_stat.n + 1")
                        .params(msg.eventId(), msg.occurredAt().truncatedTo(ChronoUnit.SECONDS).atOffset(ZoneOffset.UTC), metric).update();
            }
        });
    }

    /** A message that will never succeed however often it is retried. Goes straight to the dead-letter table. */
    public static class PoisonMessageException extends RuntimeException {
        public PoisonMessageException(String message) { super(message); }
    }
}
