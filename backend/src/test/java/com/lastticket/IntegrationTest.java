package com.lastticket;

import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.kafka.KafkaContainer;

/**
 * Base for tests that need the real dependencies. Containers are started once per JVM and shared
 * (singleton pattern) so the suite pays the startup cost once.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
public abstract class IntegrationTest {

    @SuppressWarnings("resource")
    static final PostgreSQLContainer<?> POSTGRES = new PostgreSQLContainer<>("postgres:16-alpine")
            .withDatabaseName("lastticket").withUsername("lastticket_owner").withPassword("owner-pw");
    @SuppressWarnings("resource")
    static final GenericContainer<?> REDIS = new GenericContainer<>("redis:7-alpine").withExposedPorts(6379);
    static final KafkaContainer KAFKA = new KafkaContainer("apache/kafka:3.9.1");

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
        r.add("lastticket.admin-api-key", () -> "test-admin-key");
        r.add("logging.structured.format.console", () -> "");
    }
}
