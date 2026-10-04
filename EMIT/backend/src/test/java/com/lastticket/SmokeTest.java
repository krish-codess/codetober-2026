package com.lastticket;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.actuate.health.HealthEndpoint;
import org.springframework.boot.actuate.health.Status;

class SmokeTest extends IntegrationTest {

    @Autowired HealthEndpoint health;

    @Test
    void contextStartsAndDependenciesAreHealthy() {
        assertThat(health.health().getStatus()).isEqualTo(Status.UP);
    }
}
