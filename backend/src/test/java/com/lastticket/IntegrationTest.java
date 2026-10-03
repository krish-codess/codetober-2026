package com.lastticket;

import com.lastticket.inventory.InventoryQueries;
import com.lastticket.inventory.InventoryService;
import com.lastticket.inventory.InventoryService.NewEvent;
import com.lastticket.inventory.InventoryService.NewInventory;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.kafka.KafkaContainer;

/**
 * Base for tests that need the real dependencies. Containers are started once per JVM and shared
 * (singleton pattern) so the suite pays the startup cost once. Tests never share events: each creates its own.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
public abstract class IntegrationTest {

    @SuppressWarnings("resource")
    static final PostgreSQLContainer<?> POSTGRES = new PostgreSQLContainer<>("postgres:16-alpine")
            .withDatabaseName("lastticket").withUsername("lastticket_owner").withPassword("owner-pw");
    @SuppressWarnings("resource")
    static final GenericContainer<?> REDIS = new GenericContainer<>("redis:7-alpine").withExposedPorts(6379);
    static final KafkaContainer KAFKA = new KafkaContainer("apache/kafka:3.9.1");

    static final String ADMIN_KEY = "test-admin-key";

    static {
        POSTGRES.start();
        REDIS.start();
        KAFKA.start();
    }

    @DynamicPropertySource
    static void properties(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", POSTGRES::getJdbcUrl);
        r.add("spring.datasource.password", () -> "app-pw");
        r.add("spring.flyway.user", POSTGRES::getUsername);
        r.add("spring.flyway.password", POSTGRES::getPassword);
        r.add("spring.flyway.placeholders.app_password", () -> "app-pw");
        r.add("spring.data.redis.host", REDIS::getHost);
        r.add("spring.data.redis.port", () -> REDIS.getMappedPort(6379));
        r.add("spring.kafka.bootstrap-servers", KAFKA::getBootstrapServers);
        r.add("lastticket.jwt-secret", () -> "test-secret-test-secret-test-secret-32b");
        r.add("lastticket.admin-api-key", () -> ADMIN_KEY);
        // Tests drive the sweeper by hand so its timing is deterministic.
        r.add("lastticket.sweeper-interval-ms", () -> "3600000");
    }

    @Autowired protected InventoryService inventory;
    @Autowired protected InventoryQueries queries;
    @Autowired protected JdbcClient db;

    /** A fresh single-section event. @return the section's inventory id */
    protected UUID newSection(int total, int onSaleInSeconds, int holdSeconds, int maxPerUser, int admissionRate) {
        UUID eventId = UUID.randomUUID();
        inventory.createEvent(new NewEvent(eventId, "test " + eventId, Instant.now().plusSeconds(onSaleInSeconds), holdSeconds,
                maxPerUser, admissionRate, List.of(new NewInventory("GA", "FLOOR", 5000, total))));
        return queries.availability(eventId).sections().get(0).inventoryId();
    }

    protected UUID newSection(int total) {
        return newSection(total, -60, 120, 4, 100);
    }

    protected InventoryQueries.Position position(UUID inventoryId) {
        return queries.positionAsOf(inventory.eventOf(inventoryId), Instant.now().plusSeconds(1)).get(0);
    }

    /** Makes a hold overdue without waiting for it (expires_at must stay after created_at, so both move). */
    protected void makeOverdue(UUID reservationId) {
        db.sql("UPDATE reservation SET created_at = now() - interval '10 minutes', expires_at = now() - interval '1 second' WHERE id = ?")
                .param(reservationId).update();
    }
}
