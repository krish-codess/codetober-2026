package com.lastticket;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.lastticket.api.ApiException;
import com.lastticket.inventory.ExpirySweeper;
import com.lastticket.inventory.Inventory;
import com.lastticket.inventory.InventoryQueries.Position;
import com.lastticket.inventory.Reservation;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

/** Requirements 2 (expiry), 5 (stock events don't break holds) and 6 (point-in-time reconstruction). */
class HoldLifecycleTest extends IntegrationTest {

    @Autowired ExpirySweeper sweeper;

    @Test
    void abandonedHoldCannotBeConfirmedAfterItsDeadline_evenBeforeTheSweeperRuns() {
        UUID section = newSection(5);
        Reservation r = inventory.reserve("ghost", section, 2, "ghost-key-1");
        makeOverdue(r.id());

        assertThatThrownBy(() -> inventory.confirm("ghost", r.id())).extracting("code").isEqualTo("HOLD_EXPIRED");
        assertThat(position(section).sold()).isZero();
    }

    @Test
    void sweeperReleasesOverdueHoldsExactlyOnce_andLeavesLiveOnesAlone() {
        UUID section = newSection(5);
        Reservation abandoned = inventory.reserve("ghost", section, 2, "ghost-key-2");
        Reservation live = inventory.reserve("shopper", section, 1, "shopper-key-2");
        makeOverdue(abandoned.id());

        sweeper.sweepOnce();
        sweeper.sweepOnce(); // a second pass must find nothing to do

        assertThat(inventory.find("ghost", abandoned.id()).status()).isEqualTo("EXPIRED");
        assertThat(inventory.find("shopper", live.id()).status()).isEqualTo("HELD");
        assertThat(position(section)).extracting(Position::held, Position::available).containsExactly(1, 4);
        assertThat(eventTypes(section)).containsExactly("STOCK_ADDED", "HOLD_PLACED", "HOLD_PLACED", "HOLD_EXPIRED");
    }

    @Test
    void confirmIsIdempotent_andReleaseAfterConfirmIsRefused() {
        UUID section = newSection(5);
        Reservation r = inventory.reserve("buyer", section, 2, "buyer-key-3");

        assertThat(inventory.confirm("buyer", r.id()).status()).isEqualTo("CONFIRMED");
        assertThat(inventory.confirm("buyer", r.id()).status()).isEqualTo("CONFIRMED");
        assertThatThrownBy(() -> inventory.release("buyer", r.id())).extracting("code").isEqualTo("ALREADY_CLOSED");
        assertThat(position(section)).extracting(Position::held, Position::sold).containsExactly(0, 2);
        assertThat(eventTypes(section)).containsExactly("STOCK_ADDED", "HOLD_PLACED", "HOLD_CONFIRMED");
    }

    @Test
    void confirmAndReleaseRacing_exactlyOneWins() throws Exception {
        for (int round = 0; round < 10; round++) {
            UUID section = newSection(5);
            Reservation r = inventory.reserve("torn", section, 2, "torn-key-" + round);
            ReservationConcurrencyTest.runTogether(2, i -> () -> {
                try {
                    return i == 0 ? inventory.confirm("torn", r.id()) : inventory.release("torn", r.id());
                } catch (ApiException e) {
                    return null;
                }
            });
            Position p = position(section);
            assertThat(p.held()).isZero();
            assertThat(p.sold()).isIn(0, 2);
            assertThat(inventory.find("torn", r.id()).status()).isEqualTo(p.sold() == 2 ? "CONFIRMED" : "RELEASED");
        }
    }

    @Test
    void perPersonLimitCountsConfirmedTickets() {
        UUID section = newSection(20); // max 4 per user
        inventory.confirm("fan", inventory.reserve("fan", section, 3, "fan-key-a").id());
        assertThatThrownBy(() -> inventory.reserve("fan", section, 2, "fan-key-b")).extracting("code").isEqualTo("LIMIT_EXCEEDED");
        assertThat(inventory.reserve("fan", section, 1, "fan-key-c").status()).isEqualTo("HELD");
    }

    @Test
    void someoneElsesReservationLooksLikeItDoesNotExist() {
        UUID section = newSection(5);
        Reservation r = inventory.reserve("owner", section, 1, "owner-key-1");
        assertThatThrownBy(() -> inventory.confirm("thief", r.id())).extracting("code").isEqualTo("RESERVATION_NOT_FOUND");
        assertThatThrownBy(() -> inventory.release("thief", r.id())).extracting("code").isEqualTo("RESERVATION_NOT_FOUND");
    }

    @Test
    void notOnSaleYet() {
        UUID section = newSection(5, 3600, 120, 4, 10);
        assertThatThrownBy(() -> inventory.reserve("early", section, 1, "early-key-1")).extracting("code").isEqualTo("NOT_ON_SALE");
    }

    @Test
    void stockCorrectionsAndReleasesAreEvents_andNeverBreakAnOutstandingHold() {
        UUID section = newSection(10);
        Reservation r = inventory.reserve("holder", section, 3, "holder-key-1");
        long v = position(section).version();

        // Cannot pull stock out from under the hold...
        assertThatThrownBy(() -> inventory.adjustStock(section, -8, v, "venue removed a row"))
                .extracting("code").isEqualTo("CORRECTION_BELOW_COMMITTED");
        // ...can correct down to exactly what is promised...
        Inventory corrected = inventory.adjustStock(section, -7, v, "venue removed a row");
        assertThat(corrected).extracting(Inventory::total, Inventory::held, Inventory::available).containsExactly(3, 3, 0);
        // ...a retry of the same request is refused instead of applied twice...
        assertThatThrownBy(() -> inventory.adjustStock(section, -7, v, "venue removed a row")).extracting("code").isEqualTo("VERSION_MISMATCH");
        // ...and the hold is still honoured.
        assertThat(inventory.confirm("holder", r.id()).status()).isEqualTo("CONFIRMED");

        Inventory released = inventory.adjustStock(section, 5, position(section).version(), "production holds released");
        assertThat(released).extracting(Inventory::total, Inventory::sold, Inventory::available).containsExactly(8, 3, 5);
        assertThat(eventTypes(section)).containsExactly("STOCK_ADDED", "HOLD_PLACED", "STOCK_CORRECTED", "HOLD_CONFIRMED", "STOCK_ADDED");
    }

    @Test
    void positionAtAnyPastInstantIsRebuiltFromTheStream() throws Exception {
        UUID section = newSection(10);
        UUID eventId = inventory.eventOf(section);
        Instant t0 = dbNow();
        Thread.sleep(20);
        Reservation r = inventory.reserve("time-traveller", section, 4, "tt-key-1");
        Instant t1 = dbNow();
        Thread.sleep(20);
        inventory.confirm("time-traveller", r.id());
        Instant t2 = dbNow();
        Thread.sleep(20);
        inventory.adjustStock(section, -2, position(section).version(), "correction");
        Instant t3 = dbNow();

        assertThat(queries.positionAsOf(eventId, t0.minusSeconds(3600)).get(0)).extracting(Position::total, Position::version).containsExactly(0, 0L);
        assertThat(queries.positionAsOf(eventId, t0).get(0)).extracting(Position::total, Position::held, Position::sold, Position::available).containsExactly(10, 0, 0, 10);
        assertThat(queries.positionAsOf(eventId, t1).get(0)).extracting(Position::total, Position::held, Position::sold, Position::available).containsExactly(10, 4, 0, 6);
        assertThat(queries.positionAsOf(eventId, t2).get(0)).extracting(Position::total, Position::held, Position::sold, Position::available).containsExactly(10, 0, 4, 6);
        assertThat(queries.positionAsOf(eventId, t3).get(0)).extracting(Position::total, Position::held, Position::sold, Position::available).containsExactly(8, 0, 4, 4);
    }

    @Test
    void appRoleCannotRewriteHistory() {
        UUID section = newSection(1);
        assertThatThrownBy(() -> db.sql("UPDATE inventory_event SET total_delta = 100 WHERE inventory_id = ?").param(section).update())
                .hasStackTraceContaining("permission denied");
        assertThatThrownBy(() -> db.sql("DELETE FROM inventory_event WHERE inventory_id = ?").param(section).update())
                .hasStackTraceContaining("permission denied");
        assertThatThrownBy(() -> db.sql("UPDATE inventory SET sold = 2 WHERE id = ?").param(section).update())
                .as("the database itself refuses an oversold row").hasStackTraceContaining("inventory_no_oversell");
    }

    private Instant dbNow() {
        return db.sql("SELECT clock_timestamp()").query(java.time.OffsetDateTime.class).single().toInstant();
    }

    private List<String> eventTypes(UUID section) {
        return queries.stream(section, 0, 100).stream().map(e -> e.type()).toList();
    }
}
