package com.lastticket.waitingroom;

import com.lastticket.api.ApiException;
import com.lastticket.config.AppProperties;
import com.lastticket.config.Tokens;
import com.lastticket.inventory.InventoryQueries;
import com.lastticket.inventory.InventoryQueries.EventView;
import io.micrometer.core.instrument.MeterRegistry;
import io.swagger.v3.oas.annotations.media.Schema;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.function.Supplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.data.redis.core.script.RedisScript;
import org.springframework.http.HttpStatus;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

/**
 * Virtual waiting room, entirely in Redis.
 *
 * <pre>
 * wr:{eventId}:q            ZSET  member = user id, score = place in line          TTL 24 h, refreshed on join
 * wr:{eventId}:adm:{user}   STRING value = admission expiry (epoch ms)             TTL = admission window
 * wr:{eventId}:tick:{sec}   STRING guard so only one instance admits per second    TTL 5 s
 * </pre>
 *
 * Fairness: everyone who joins before the sale opens gets a uniformly random score in [0, 1), so being early or
 * refreshing hard buys nothing; everyone who joins after gets 1 + milliseconds-since-opening, i.e. FIFO behind the
 * lobby. Joining twice is a no-op (ZADD NX), so a refresh never moves you.
 *
 * Redis is disposable. If it is wiped, queued users are told NOT_IN_QUEUE and rejoin (FIFO at the back of nobody);
 * admission tokens already issued are signed and keep working; inventory correctness never depended on Redis.
 * If Redis is unreachable the waiting room fails closed with a retryable 503: nobody new is admitted.
 */
@Service
public class WaitingRoom {
    private static final Logger log = LoggerFactory.getLogger(WaitingRoom.class);

    public enum State { LOBBY, QUEUED, ADMITTED, NOT_IN_QUEUE }

    public record Status(
            State state,
            @Schema(description = "1-based place in line. Only present when QUEUED: before the sale opens there is no order yet.")
            Integer position,
            @Schema(description = "Seconds until admission at the current admission rate. A floor, not a promise: present when QUEUED or LOBBY (time until the sale opens).")
            Long estimatedWaitSeconds,
            @Schema(description = "Present when ADMITTED. Send as X-Admission-Token when reserving.") String admissionToken,
            Instant admissionExpiresAt,
            Instant onSaleAt,
            @Schema(description = "How long the client should wait before polling again.") long pollAfterMs) {}

    // KEYS[1] = queue, KEYS[2] = this user's admission key; ARGV[1] = user, ARGV[2] = score. Returns rank, or -1 if admitted.
    private static final RedisScript<Long> JOIN = new DefaultRedisScript<>("""
            if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
            redis.call('ZADD', KEYS[1], 'NX', ARGV[2], ARGV[1])
            redis.call('EXPIRE', KEYS[1], 86400)
            return redis.call('ZRANK', KEYS[1], ARGV[1])
            """, Long.class);

    // KEYS[1] = queue, KEYS[2] = tick guard; ARGV = count, admission ttl (s), admission key prefix, expiry (epoch ms).
    // Pop + mark admitted atomically: a crash can never leave someone out of the queue but not admitted.
    // The admission keys are built from a prefix; they share the {eventId} hash tag with KEYS, so this is cluster-safe.
    private static final RedisScript<Long> ADMIT = new DefaultRedisScript<>("""
            if not redis.call('SET', KEYS[2], '1', 'NX', 'EX', 5) then return 0 end
            local popped = redis.call('ZPOPMIN', KEYS[1], ARGV[1])
            for i = 1, #popped, 2 do
              redis.call('SET', ARGV[3] .. popped[i], ARGV[4], 'EX', ARGV[2])
            end
            return #popped / 2
            """, Long.class);

    private final StringRedisTemplate redis;
    private final InventoryQueries queries;
    private final Tokens tokens;
    private final AppProperties props;
    private final MeterRegistry metrics;
    private final SecureRandom random = new SecureRandom();

    public WaitingRoom(StringRedisTemplate redis, InventoryQueries queries, Tokens tokens, AppProperties props, MeterRegistry metrics) {
        this.redis = redis;
        this.queries = queries;
        this.tokens = tokens;
        this.props = props;
        this.metrics = metrics;
    }

    /** Idempotent: joining again returns the place you already have. */
    public Status join(UUID eventId, String userId) {
        EventView event = queries.event(eventId);
        long sinceOpenMs = event.serverTime().toEpochMilli() - event.onSaleAt().toEpochMilli();
        double score = sinceOpenMs < 0 ? random.nextDouble() : 1 + sinceOpenMs;
        Long rank = guarded(() -> redis.execute(JOIN, List.of(queueKey(eventId), admissionKey(eventId, userId)), userId, Double.toString(score)));
        return rank == null || rank < 0 ? status(eventId, userId) : queued(event, rank);
    }

    public Status status(UUID eventId, String userId) {
        EventView event = queries.event(eventId);
        String admittedUntil = guarded(() -> redis.opsForValue().get(admissionKey(eventId, userId)));
        if (admittedUntil != null) {
            Instant expires = Instant.ofEpochMilli(Long.parseLong(admittedUntil));
            return new Status(State.ADMITTED, null, null, tokens.admission(userId, eventId, expires), expires, event.onSaleAt(), 5000);
        }
        Long rank = guarded(() -> redis.opsForZSet().rank(queueKey(eventId), userId));
        if (rank == null) {
            return new Status(State.NOT_IN_QUEUE, null, null, null, null, event.onSaleAt(), 0);
        }
        return queued(event, rank);
    }

    private Status queued(EventView event, long rank) {
        long untilOpenMs = event.onSaleAt().toEpochMilli() - event.serverTime().toEpochMilli();
        if (untilOpenMs > 0) {
            // No position yet: the order among lobby members only becomes final when the sale opens.
            return new Status(State.LOBBY, null, (untilOpenMs + 999) / 1000, null, null, event.onSaleAt(), Math.min(5000, Math.max(1000, untilOpenMs)));
        }
        long waitSeconds = rank / event.admissionRatePerSec() + 1;
        // People far back poll less often: polling load shrinks instead of growing with queue length.
        long pollMs = Math.max(1000, Math.min(10_000, waitSeconds * 250));
        return new Status(State.QUEUED, (int) rank + 1, waitSeconds, null, null, event.onSaleAt(), pollMs);
    }

    /** Admits up to the event's per-second rate. Safe to call from every instance: the tick guard dedupes. */
    @Scheduled(fixedRate = 1000)
    public void admitTick() {
        try {
            for (EventView event : queries.onSaleNow()) {
                admit(event);
            }
        } catch (RuntimeException e) {
            log.warn("admission tick failed, will retry next second: {}", e.toString());
        }
    }

    long admit(EventView event) {
        long nowMs = System.currentTimeMillis();
        long expires = nowMs + props.admissionTtlSeconds() * 1000L;
        Long admitted = redis.execute(ADMIT, List.of(queueKey(event.id()), "wr:{" + event.id() + "}:tick:" + nowMs / 1000),
                Integer.toString(event.admissionRatePerSec()), Integer.toString(props.admissionTtlSeconds()),
                "wr:{" + event.id() + "}:adm:", Long.toString(expires));
        if (admitted != null && admitted > 0) {
            metrics.counter("lastticket.waitingroom.admitted").increment(admitted);
        }
        return admitted == null ? 0 : admitted;
    }

    private static String queueKey(UUID eventId) {
        return "wr:{" + eventId + "}:q";
    }

    private static String admissionKey(UUID eventId, String userId) {
        return "wr:{" + eventId + "}:adm:" + userId;
    }

    private static <T> T guarded(Supplier<T> redisCall) {
        try {
            return redisCall.get();
        } catch (DataAccessException e) {
            log.warn("waiting room unavailable: {}", e.toString());
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "WAITING_ROOM_UNAVAILABLE",
                    "The queue is temporarily unavailable. You have not lost anything; we will retry automatically.", 5);
        }
    }
}
