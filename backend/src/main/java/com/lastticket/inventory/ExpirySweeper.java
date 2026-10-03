package com.lastticket.inventory;

import io.micrometer.core.instrument.MeterRegistry;
import java.util.List;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * Releases holds whose deadline has passed. Nothing depends on the client coming back: the deadline lives in the
 * database. Each hold is expired in its own transaction guarded by {@code status = 'HELD'}, so a crash mid-batch loses
 * nothing (the uncommitted one is still due next tick) and several instances can sweep at once without double release.
 */
@Component
public class ExpirySweeper {
    private static final Logger log = LoggerFactory.getLogger(ExpirySweeper.class);
    private static final int BATCH = 200;

    private final JdbcClient db;
    private final InventoryService inventory;
    private final MeterRegistry metrics;

    public ExpirySweeper(JdbcClient db, InventoryService inventory, MeterRegistry metrics) {
        this.db = db;
        this.inventory = inventory;
        this.metrics = metrics;
    }

    @Scheduled(fixedDelayString = "${lastticket.sweeper-interval-ms}")
    public void sweep() {
        try {
            sweepOnce();
        } catch (RuntimeException e) {
            // Database unavailable: nothing to do but try again next tick. Expired holds can never be confirmed anyway.
            log.warn("sweep failed, will retry next tick: {}", e.toString());
        }
    }

    /** @return holds expired by this call */
    public int sweepOnce() {
        int expired = 0;
        List<UUID> due;
        do {
            due = db.sql("SELECT id FROM reservation WHERE status = 'HELD' AND expires_at <= now() ORDER BY expires_at LIMIT ?")
                    .param(BATCH).query(UUID.class).list();
            int progressed = 0;
            for (UUID id : due) {
                try {
                    if (inventory.expire(id)) {
                        progressed++;
                    }
                } catch (RuntimeException e) {
                    log.warn("could not expire reservation {}, leaving it for the next tick: {}", id, e.toString());
                }
            }
            expired += progressed;
            if (progressed == 0) {
                break; // every candidate failed or was taken by another instance: don't spin
            }
        } while (due.size() == BATCH);
        if (expired > 0) {
            metrics.counter("lastticket.sweeper.expired").increment(expired);
            log.info("expired {} holds", expired);
        }
        return expired;
    }
}
