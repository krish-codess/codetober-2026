package com.lastticket.messaging;

import com.lastticket.api.ApiException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.concurrent.TimeUnit;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Service;

/** Read and replay the dead-letter table (written by the Kafka error handler in {@link KafkaConfig}). */
@Service
public class DeadLetters {
    public record DeadLetter(long id, String topic, int partition, long offset, String key, String payload, String reason,
                             Instant failedAt, Instant replayedAt) {}

    private static final String COLUMNS = "id, topic, kafka_partition, kafka_offset, msg_key, payload, reason, failed_at, replayed_at";

    private final JdbcClient db;
    private final KafkaTemplate<String, String> kafka;

    public DeadLetters(JdbcClient db, KafkaTemplate<String, String> kafka) {
        this.db = db;
        this.kafka = kafka;
    }

    public List<DeadLetter> list(long afterId, int limit) {
        return db.sql("SELECT " + COLUMNS + " FROM dead_letter WHERE id > ? ORDER BY id LIMIT ?").params(afterId, limit)
                .query(DeadLetters::map).list();
    }

    /** Re-publishes the original payload to the original topic. Only useful once whatever made it fail is fixed. */
    public DeadLetter replay(long id) {
        DeadLetter d = db.sql("SELECT " + COLUMNS + " FROM dead_letter WHERE id = ?").param(id).query(DeadLetters::map).optional()
                .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "DEAD_LETTER_NOT_FOUND", "No such dead letter."));
        try {
            kafka.send(d.topic(), d.key(), d.payload()).get(5, TimeUnit.SECONDS);
        } catch (Exception e) {
            if (e instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "KAFKA_UNAVAILABLE", "Could not reach Kafka; the dead letter is unchanged. Retry.", 5);
        }
        return db.sql("UPDATE dead_letter SET replayed_at = now() WHERE id = ? RETURNING " + COLUMNS).param(id).query(DeadLetters::map).single();
    }

    private static DeadLetter map(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        OffsetDateTime replayed = rs.getObject("replayed_at", OffsetDateTime.class);
        return new DeadLetter(rs.getLong("id"), rs.getString("topic"), rs.getInt("kafka_partition"), rs.getLong("kafka_offset"),
                rs.getString("msg_key"), rs.getString("payload"), rs.getString("reason"),
                rs.getObject("failed_at", OffsetDateTime.class).toInstant(), replayed == null ? null : replayed.toInstant());
    }
}
