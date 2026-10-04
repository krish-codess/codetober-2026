package com.lastticket;

import static org.assertj.core.api.Assertions.assertThat;

import com.lastticket.api.ApiException;
import com.lastticket.inventory.InventoryQueries.Position;
import com.lastticket.inventory.Reservation;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;
import org.junit.jupiter.api.Test;
import org.springframework.test.context.TestPropertySource;

/**
 * Requirement 1: no oversell under concurrent reservation attempts.
 * Runs with the in-process gate opened to 16 writers per aggregate (as if 16 instances were racing; kept below the 20-connection pool), so what is under
 * test is the optimistic version check on its own. Every other test class runs with the production default of 1.
 */
@TestPropertySource(properties = "lastticket.writers-per-aggregate=16")
class ReservationConcurrencyTest extends IntegrationTest {

    @org.springframework.beans.factory.annotation.Autowired io.micrometer.core.instrument.MeterRegistry metrics;

    @Test
    void manyBuyersFewTickets_neverOversells_andEveryAttemptGetsADefiniteAnswer() throws Exception {
        int stock = 60, buyers = 400;
        UUID section = newSection(stock);
        AtomicInteger heldTickets = new AtomicInteger(), soldOut = new AtomicInteger(), contention = new AtomicInteger();

        runTogether(buyers, i -> () -> {
            int qty = 1 + i % 3; // deterministic mix of 1, 2, 3
            while (true) {
                try {
                    heldTickets.addAndGet(inventory.reserve("buyer-" + i, section, qty, "key-" + i).quantity());
                    return null;
                } catch (ApiException e) {
                    switch (e.code()) {
                        case "SOLD_OUT" -> {
                            soldOut.incrementAndGet();
                            return null;
                        }
                        case "CONTENTION" -> contention.incrementAndGet(); // 503: nothing happened; retry like a client would
                        default -> throw e;
                    }
                }
            }
        });
        double conflicts = metrics.counter("lastticket.version.conflicts").count();
        System.out.println("ungated: " + buyers + " simultaneous buyers -> " + (long) conflicts + " version conflicts, " + contention.get() + " CONTENTION responses");
        assertThat(conflicts).as("the race really happened and the version check really arbitrated it").isPositive();

        Position p = position(section);
        assertThat(p.held()).as("held never exceeds stock").isLessThanOrEqualTo(stock);
        assertThat(p.held()).as("snapshot matches what callers were told").isEqualTo(heldTickets.get());
        assertThat(p.sold()).isZero();
        assertThat(soldOut.get()).as("demand far exceeded supply").isPositive();
        // Anything left over is smaller than the smallest request that was refused for it: the sale sold through.
        assertThat(p.available()).isLessThan(3);
        int heldInTable = db.sql("SELECT coalesce(sum(quantity), 0) FROM reservation WHERE inventory_id = ? AND status = 'HELD'")
                .param(section).query(Integer.class).single();
        assertThat(heldInTable).as("reservation rows agree with the aggregate").isEqualTo(p.held());
        assertThat(queries.analytics(inventory.eventOf(section))).as("stream replay equals snapshot; nothing oversold")
                .extracting("oversoldTickets", "snapshotDrift").containsExactly(0, 0);
    }

    @Test
    void theSameRequestSentManyTimesAtOnceCreatesOneReservation() throws Exception {
        UUID section = newSection(10);
        List<Reservation> results = runTogether(30, i -> () -> inventory.reserve("impatient", section, 2, "same-key-0001"));

        Set<UUID> ids = results.stream().map(Reservation::id).collect(Collectors.toSet());
        assertThat(ids).hasSize(1);
        assertThat(position(section).held()).isEqualTo(2);
    }

    @Test
    void oneUserCannotHoardWithParallelHolds() throws Exception {
        UUID section = newSection(100);
        AtomicInteger refused = new AtomicInteger();
        runTogether(20, i -> () -> {
            try {
                return inventory.reserve("hoarder", section, 2, "hoard-key-" + i);
            } catch (ApiException e) {
                assertThat(e.code()).isIn("ALREADY_HOLDING", "CONTENTION");
                refused.incrementAndGet();
                return null;
            }
        });
        assertThat(refused.get()).isEqualTo(19);
        assertThat(position(section).held()).isEqualTo(2);
    }

    /** Releases all tasks at the same instant, the way an on-sale does. */
    static <T> List<T> runTogether(int n, java.util.function.IntFunction<Callable<T>> task) throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(64);
        try {
            CountDownLatch go = new CountDownLatch(1);
            List<Future<T>> futures = new ArrayList<>();
            for (int i = 0; i < n; i++) {
                Callable<T> c = task.apply(i);
                futures.add(pool.submit(() -> {
                    go.await();
                    return c.call();
                }));
            }
            go.countDown();
            List<T> out = new ArrayList<>();
            for (Future<T> f : futures) {
                out.add(f.get());
            }
            return out;
        } finally {
            pool.shutdownNow();
        }
    }
}
