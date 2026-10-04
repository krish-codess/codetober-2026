package com.lastticket;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.awaitility.Awaitility.await;

import com.lastticket.config.Tokens;
import com.lastticket.waitingroom.WaitingRoom;
import com.lastticket.waitingroom.WaitingRoom.State;
import com.lastticket.waitingroom.WaitingRoom.Status;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.data.redis.core.StringRedisTemplate;

/** Requirement 3: admission is fair and rate-controlled. Plus: Redis may vanish. */
class WaitingRoomTest extends IntegrationTest {

    @Autowired WaitingRoom room;
    @Autowired Tokens tokens;
    @Autowired StringRedisTemplate redis;

    @Test
    void lobbyOrderIsALottery_lateArrivalsQueueBehindIt_andRefreshingChangesNothing() {
        // Opens in 3 s; admits 1/s so the queue is still observable after it opens.
        UUID section = newSection(10, 3, 120, 4, 1);
        UUID event = inventory.eventOf(section);

        int lobbySize = 200;
        for (int i = 0; i < lobbySize; i++) {
            Status s = room.join(event, "early-" + i);
            assertThat(s.state()).isEqualTo(State.LOBBY);
            assertThat(s.position()).as("no position is promised before the sale opens").isNull();
        }

        await().atMost(Duration.ofSeconds(10)).until(() -> queries.event(event).serverTime().isAfter(queries.event(event).onSaleAt()));
        Status late1 = room.join(event, "late-1");
        Status late2 = room.join(event, "late-2");

        List<Integer> positions = new ArrayList<>();
        for (int i = 0; i < lobbySize; i++) {
            Status s = room.status(event, "early-" + i);
            if (s.state() == State.QUEUED) {
                positions.add(s.position());
            }
        }
        // Arriving first bought nothing: the first 20 to arrive are not the first 20 in line.
        // (Chance of this failing by luck: 1 in C(200,20) ~ 10^-27.)
        assertThat(positions.subList(0, 20).stream().filter(p -> p <= 20).count()).isLessThan(20);

        // After opening it is first come, first served, behind everyone from the lobby.
        assertThat(late1.state()).isEqualTo(State.QUEUED);
        assertThat(late2.position()).isGreaterThan(late1.position());
        assertThat(late1.position()).isGreaterThan(positions.stream().mapToInt(Integer::intValue).max().orElseThrow());

        // Hammering join does not move you (other than forward, as people ahead are admitted).
        int before = room.status(event, "late-2").position();
        for (int i = 0; i < 50; i++) {
            room.join(event, "late-2");
        }
        assertThat(room.status(event, "late-2").position()).isLessThanOrEqualTo(before).isGreaterThan(before - 5);
        assertThat(room.status(event, "late-2").estimatedWaitSeconds()).isGreaterThan(100);
    }

    @Test
    void admissionIsRateLimited_andAnAdmissionTokenOnlyWorksForItsOwnerAndEvent() {
        UUID section = newSection(10, -1, 120, 4, 5); // already open, 5 admissions per second
        UUID event = inventory.eventOf(section);
        for (int i = 0; i < 60; i++) {
            room.join(event, "fan-" + i);
        }

        Status first = await().atMost(Duration.ofSeconds(10)).until(() -> room.status(event, "fan-0"), s -> s.state() == State.ADMITTED);
        long admitted = java.util.stream.IntStream.range(0, 60).filter(i -> room.status(event, "fan-" + i).state() == State.ADMITTED).count();
        assertThat(admitted).as("5 per second, not a stampede").isBetween(5L, 40L);
        assertThat(room.status(event, "fan-59").state()).isEqualTo(State.QUEUED);

        assertThat(first.admissionExpiresAt()).isAfter(Instant.now());
        tokens.requireAdmission(first.admissionToken(), "fan-0", event);
        assertThatThrownBy(() -> tokens.requireAdmission(first.admissionToken(), "fan-1", event)).extracting("code").isEqualTo("NOT_ADMITTED");
        assertThatThrownBy(() -> tokens.requireAdmission(first.admissionToken(), "fan-0", UUID.randomUUID())).extracting("code").isEqualTo("NOT_ADMITTED");
        assertThatThrownBy(() -> tokens.requireAdmission(tokens.session("fan-0", Instant.now()), "fan-0", event))
                .as("a session token is not an admission token").extracting("code").isEqualTo("NOT_ADMITTED");
        assertThatThrownBy(() -> tokens.requireAdmission(tokens.admission("fan-0", event, Instant.now().minusSeconds(1)), "fan-0", event))
                .as("expired").extracting("code").isEqualTo("NOT_ADMITTED");
    }

    @Test
    void redisCanBeWiped_queueIsRebuiltByRejoining_andSalesStayCorrect() {
        UUID section = newSection(2, -1, 120, 4, 1);
        UUID event = inventory.eventOf(section);
        room.join(event, "w-1");
        Status admitted = await().atMost(Duration.ofSeconds(10)).until(() -> room.status(event, "w-1"), s -> s.state() == State.ADMITTED);
        room.join(event, "w-2");
        room.join(event, "w-3");

        redis.execute((org.springframework.data.redis.core.RedisCallback<Object>) c -> {
            c.serverCommands().flushAll();
            return null;
        });

        assertThat(room.status(event, "w-3").state()).as("told honestly, not left spinning").isEqualTo(State.NOT_IN_QUEUE);
        assertThat(room.join(event, "w-3").state()).isIn(State.QUEUED, State.ADMITTED);
        // The token issued before the wipe still admits; availability falls back to Postgres; stock is intact.
        tokens.requireAdmission(admitted.admissionToken(), "w-1", event);
        assertThat(queries.availability(event).sections().get(0).available()).isEqualTo(2);
        assertThat(inventory.reserve("w-1", section, 2, "wipe-key-1").status()).isEqualTo("HELD");
        await().atMost(Duration.ofSeconds(5)).until(() -> queries.availability(event).sections().get(0).available() == 0);
    }
}
