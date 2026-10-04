package com.lastticket;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.awaitility.Awaitility.await;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.lastticket.inventory.InventoryQueries.Analytics;
import com.lastticket.inventory.Reservation;
import com.lastticket.messaging.DeadLetters;
import com.lastticket.messaging.DeadLetters.DeadLetter;
import com.lastticket.messaging.Outbox;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.kafka.core.KafkaTemplate;

/** Outbox -> Kafka -> stats consumer: at-least-once delivery, idempotent consumption, dead letters and replay. */
class MessagingTest extends IntegrationTest {

    @Autowired KafkaTemplate<String, String> kafka;
    @Autowired ObjectMapper json;
    @Autowired DeadLetters deadLetters;
    @Autowired com.lastticket.inventory.ExpirySweeper sweeper;

    @Test
    void saleFlowsThroughKafkaIntoRates() {
        UUID section = newSection(4);
        UUID eventId = inventory.eventOf(section);
        Reservation bought = inventory.reserve("a", section, 2, "msg-key-a");
        Reservation abandoned = inventory.reserve("b", section, 2, "msg-key-b");
        assertThatThrownBy(() -> inventory.reserve("c", section, 1, "msg-key-c")).extracting("code").isEqualTo("SOLD_OUT");
        assertThatThrownBy(() -> inventory.reserve("d", section, 1, "msg-key-d")).extracting("code").isEqualTo("SOLD_OUT");
        inventory.confirm("a", bought.id());
        makeOverdue(abandoned.id());
        sweeper.sweepOnce();

        await().atMost(Duration.ofSeconds(30)).untilAsserted(() -> {
            Analytics a = queries.analytics(eventId);
            assertThat(a.attempts()).isEqualTo(4);
            assertThat(a.holds()).isEqualTo(2);
            assertThat(a.oversellAttempts()).isEqualTo(2);
            assertThat(a.oversellAttemptRate()).isEqualTo(0.5);
            assertThat(a.holdConversionRate()).isEqualTo(0.5);
            assertThat(a.holdExpiryRate()).isEqualTo(0.5);
            assertThat(a.oversoldTickets()).isZero();
            assertThat(a.snapshotDrift()).isZero();
        });
    }

    @Test
    void redeliveredMessageIsCountedOnce() throws Exception {
        UUID section = newSection(4);
        UUID eventId = inventory.eventOf(section);
        String payload = json.writeValueAsString(new Outbox.Message(UUID.randomUUID(), "HOLD_PLACED", eventId, section, UUID.randomUUID(), 1, null, Instant.now()));
        String marker = json.writeValueAsString(new Outbox.Message(UUID.randomUUID(), "HOLD_CONFIRMED", eventId, section, UUID.randomUUID(), 1, null, Instant.now()));

        // What a relay crash between "send" and "mark published" looks like downstream: the same message three times.
        for (int i = 0; i < 3; i++) {
            kafka.send(Outbox.TOPIC, section.toString(), payload).get();
        }
        kafka.send(Outbox.TOPIC, section.toString(), marker).get(); // same key => same partition => consumed after the duplicates

        await().atMost(Duration.ofSeconds(30)).until(() -> queries.analytics(eventId).confirmed() == 1);
        assertThat(queries.analytics(eventId).holds()).isEqualTo(1);
    }

    @Test
    void poisonMessageGoesToDeadLetterWithPayloadAndReason_andCanBeReplayed() throws Exception {
        String poison = "{\"this is\": \"not an event\", \"marker\": \"" + UUID.randomUUID() + "\"}";
        kafka.send(Outbox.TOPIC, "poison", poison).get();

        DeadLetter dead = await().atMost(Duration.ofSeconds(30))
                .until(() -> find(poison), d -> d != null);
        assertThat(dead.reason()).contains("PoisonMessageException").contains("missing id");
        assertThat(dead.replayedAt()).isNull();

        // Later messages on the same partition are not blocked behind the poison one.
        UUID section = newSection(1);
        kafka.send(Outbox.TOPIC, "poison", json.writeValueAsString(new Outbox.Message(UUID.randomUUID(), "HOLD_RELEASED",
                inventory.eventOf(section), section, UUID.randomUUID(), 1, null, Instant.now()))).get();
        await().atMost(Duration.ofSeconds(30)).until(() -> queries.analytics(inventory.eventOf(section)).released() == 1);

        assertThat(deadLetters.replay(dead.id()).replayedAt()).isNotNull();
        // Still poison, so it comes back as a new dead letter at a new offset: replay really re-published it.
        await().atMost(Duration.ofSeconds(30)).until(() -> deadLetters.list(0, 500).stream().filter(d -> poison.equals(d.payload())).count() == 2);
    }

    private DeadLetter find(String payload) {
        List<DeadLetter> all = deadLetters.list(0, 500);
        return all.stream().filter(d -> payload.equals(d.payload())).findFirst().orElse(null);
    }
}
