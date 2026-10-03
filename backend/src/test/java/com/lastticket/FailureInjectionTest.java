package com.lastticket;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doCallRealMethod;
import static org.mockito.Mockito.doThrow;

import com.lastticket.inventory.ExpirySweeper;
import com.lastticket.inventory.InventoryQueries.Position;
import com.lastticket.inventory.Reservation;
import com.lastticket.messaging.Outbox;
import com.lastticket.messaging.OutboxPublisher;
import java.time.Duration;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;

/**
 * Kill a worker mid-processing; assert nothing is lost and nothing is applied twice.
 * The "kill" is an exception thrown at the last step of the unit of work, after the reservation row and the
 * inventory snapshot have already been written inside the transaction, which is what a process death at that
 * point looks like to the database: the transaction never commits.
 */
class FailureInjectionTest extends IntegrationTest {

    @Autowired ExpirySweeper sweeper;
    @Autowired OutboxPublisher publisher;
    @MockitoSpyBean Outbox outbox;

    @Test
    void sweeperDyingMidExpiry_leavesTheHoldIntact_andTheNextSweepExpiresItExactlyOnce() {
        UUID section = newSection(5);
        Reservation r = inventory.reserve("ghost", section, 3, "fi-key-1");
        makeOverdue(r.id());

        doThrow(new IllegalStateException("worker killed")).when(outbox).add(eq("HOLD_EXPIRED"), any(), any(), any(), any(), any());
        assertThat(sweeper.sweepOnce()).isZero();

        // Nothing half-applied: still held, tickets still reserved, no expiry event.
        assertThat(inventory.find("ghost", r.id()).status()).isEqualTo("HELD");
        assertThat(position(section)).extracting(Position::held, Position::available).containsExactly(3, 2);
        assertThat(queries.stream(section, 0, 100)).extracting("type").containsExactly("STOCK_ADDED", "HOLD_PLACED");

        // The worker comes back.
        doCallRealMethod().when(outbox).add(any(), any(), any(), any(), any(), any());
        assertThat(sweeper.sweepOnce()).isEqualTo(1);
        assertThat(sweeper.sweepOnce()).isZero();

        assertThat(inventory.find("ghost", r.id()).status()).isEqualTo("EXPIRED");
        assertThat(position(section)).extracting(Position::held, Position::available).containsExactly(0, 5);
        assertThat(queries.stream(section, 0, 100)).extracting("type").containsExactly("STOCK_ADDED", "HOLD_PLACED", "HOLD_EXPIRED");
    }

    @Test
    void reservationDyingBeforeCommit_holdsNothing_andTheRetryWithTheSameKeySucceedsOnce() {
        UUID section = newSection(5);

        doThrow(new IllegalStateException("worker killed")).when(outbox).add(eq("HOLD_PLACED"), any(), any(), any(), any(), any());
        try {
            inventory.reserve("buyer", section, 2, "fi-key-2");
        } catch (IllegalStateException expected) {
            // the client sees a 500 and retries
        }
        assertThat(position(section).held()).isZero();

        doCallRealMethod().when(outbox).add(any(), any(), any(), any(), any(), any());
        Reservation first = inventory.reserve("buyer", section, 2, "fi-key-2");
        Reservation again = inventory.reserve("buyer", section, 2, "fi-key-2");
        assertThat(again.id()).isEqualTo(first.id());
        assertThat(position(section).held()).isEqualTo(2);
    }

    @Test
    void relayDyingAfterSendButBeforeMarking_resendsOnRestart_andDownstreamCountsOnce() {
        UUID section = newSection(5);
        UUID eventId = inventory.eventOf(section);
        inventory.reserve("buyer", section, 1, "fi-key-3");

        // Wait for the relay to publish, then pretend it died before recording that it had: un-mark the rows.
        org.awaitility.Awaitility.await().atMost(Duration.ofSeconds(30)).until(() -> queries.analytics(eventId).holds() == 1);
        int unmarked = db.sql("UPDATE outbox SET published_at = NULL WHERE msg_key = ?").param(section.toString()).update();
        assertThat(unmarked).isEqualTo(2); // STOCK_ADDED + HOLD_PLACED
        org.awaitility.Awaitility.await().atMost(Duration.ofSeconds(30)).until(() ->
                db.sql("SELECT count(*) FROM outbox WHERE msg_key = ? AND published_at IS NULL").param(section.toString()).query(Long.class).single() == 0);

        // A marker event on the same partition proves the consumer has passed the duplicates.
        inventory.release("buyer", inventory.list("buyer", null, null, 1).get(0).id());
        org.awaitility.Awaitility.await().atMost(Duration.ofSeconds(30)).until(() -> queries.analytics(eventId).released() == 1);
        assertThat(queries.analytics(eventId).holds()).as("delivered twice, counted once").isEqualTo(1);
    }
}
