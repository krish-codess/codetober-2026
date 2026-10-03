package com.lastticket.messaging;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.Instant;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;

/**
 * Transactional outbox. {@link #add} runs inside the caller's transaction, so a message exists if and only if the
 * state change it describes committed. {@link OutboxPublisher} moves rows to Kafka.
 */
@Component
public class Outbox {
    public static final String TOPIC = "ticketing.events";

    /** The wire format on {@value #TOPIC}. {@code id} is what consumers deduplicate on. */
    public record Message(UUID id, String type, UUID eventId, UUID inventoryId, UUID reservationId,
                          Integer quantity, String reason, Instant occurredAt) {}

    private final JdbcClient db;
    private final ObjectMapper json;

    public Outbox(JdbcClient db, ObjectMapper json) {
        this.db = db;
        this.json = json;
    }

    public void add(String type, UUID eventId, UUID inventoryId, UUID reservationId, Integer quantity, String reason) {
        var msg = new Message(UUID.randomUUID(), type, eventId, inventoryId, reservationId, quantity, reason, Instant.now());
        try {
            // Keyed by inventory id: one aggregate's events stay in order on one partition.
            db.sql("INSERT INTO outbox (topic, msg_key, payload) VALUES (?, ?, ?)")
                    .params(TOPIC, inventoryId.toString(), json.writeValueAsString(msg)).update();
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }
}
