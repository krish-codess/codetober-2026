package com.lastticket.messaging;

import io.micrometer.core.instrument.MeterRegistry;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Relays outbox rows to Kafka: at-least-once. A crash between send and mark re-sends the batch on restart; consumers
 * deduplicate on the message id. When Kafka is down nothing is lost and nothing upstream fails: rows accumulate and
 * the relay retries with capped exponential backoff.
 */
@Component
public class OutboxPublisher {
    private static final Logger log = LoggerFactory.getLogger(OutboxPublisher.class);
    private static final int BATCH = 500;
    private static final long MAX_BACKOFF_MS = 30_000;
    private static final long LOCK_ID = 0x1A57_71C7L;

    private record Row(long id, String topic, String key, String payload) {}

    private final JdbcClient db;
    private final TransactionTemplate tx;
    private final KafkaTemplate<String, String> kafka;
    private final MeterRegistry metrics;
    private int consecutiveFailures;
    private long retryNotBefore;

    public OutboxPublisher(JdbcClient db, TransactionTemplate tx, KafkaTemplate<String, String> kafka, MeterRegistry metrics) {
        this.db = db;
        this.tx = tx;
        this.kafka = kafka;
        this.metrics = metrics;
    }

    @Scheduled(fixedDelayString = "${lastticket.outbox-interval-ms}")
    public void tick() {
        if (System.currentTimeMillis() < retryNotBefore) {
            return;
        }
        try {
            while (publishBatch() == BATCH) { /* drain */ }
            consecutiveFailures = 0;
        } catch (RuntimeException e) {
            long backoff = Math.min(MAX_BACKOFF_MS, 250L << Math.min(consecutiveFailures++, 10));
            retryNotBefore = System.currentTimeMillis() + backoff;
            metrics.counter("lastticket.outbox.failures").increment();
            log.warn("outbox publish failed, retrying in {} ms: {}", backoff, e.toString());
        }
    }

    /** @return rows published */
    public int publishBatch() {
        Integer published = tx.execute(s -> {
            // One relay at a time across all instances, so per-key order in Kafka matches outbox order.
            // ponytail: single relay holding a transaction open across the sends; partition the outbox by key hash
            // and run one relay per partition when one relay stops keeping up.
            if (!db.sql("SELECT pg_try_advisory_xact_lock(?)").param(LOCK_ID).query(Boolean.class).single()) {
                return 0;
            }
            List<Row> rows = db.sql("SELECT id, topic, msg_key, payload FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT ?")
                    .param(BATCH)
                    .query((rs, n) -> new Row(rs.getLong("id"), rs.getString("topic"), rs.getString("msg_key"), rs.getString("payload"))).list();
            if (rows.isEmpty()) {
                return 0;
            }
            List<CompletableFuture<?>> sends = new ArrayList<>(rows.size());
            for (Row r : rows) {
                sends.add(kafka.send(r.topic(), r.key(), r.payload()));
            }
            try {
                CompletableFuture.allOf(sends.toArray(CompletableFuture[]::new)).get(10, TimeUnit.SECONDS);
            } catch (Exception e) {
                if (e instanceof InterruptedException) {
                    Thread.currentThread().interrupt();
                }
                // Mark nothing: some of the batch may have been sent and will be sent again. That is the at-least-once.
                throw new IllegalStateException("kafka send failed: " + e, e);
            }
            // Exactly the ids we sent: a range would also swallow rows committed by slower transactions in between.
            db.sql("UPDATE outbox SET published_at = now() WHERE id = ANY(?)")
                    .param(rows.stream().map(Row::id).toArray(Long[]::new)).update();
            return rows.size();
        });
        int n = published == null ? 0 : published;
        if (n > 0) {
            metrics.counter("lastticket.outbox.published").increment(n);
        }
        return n;
    }
}
