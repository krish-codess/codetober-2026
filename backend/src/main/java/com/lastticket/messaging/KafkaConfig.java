package com.lastticket.messaging;

import java.time.Duration;
import java.util.concurrent.TimeUnit;
import org.apache.kafka.clients.admin.AdminClient;
import org.apache.kafka.clients.admin.NewTopic;
import org.apache.kafka.common.config.TopicConfig;
import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.kafka.config.TopicBuilder;
import org.springframework.kafka.core.KafkaAdmin;
import org.springframework.kafka.listener.CommonErrorHandler;
import org.springframework.kafka.listener.DefaultErrorHandler;
import org.springframework.kafka.support.ExponentialBackOffWithMaxRetries;

@Configuration
class KafkaConfig {

    /** 6 partitions keyed by inventory id (per-aggregate order); 7 days retention. */
    @Bean
    NewTopic ticketingEvents() {
        return TopicBuilder.name(Outbox.TOPIC).partitions(6).replicas(1)
                .config(TopicConfig.RETENTION_MS_CONFIG, String.valueOf(Duration.ofDays(7).toMillis())).build();
    }

    /**
     * Transient failures (database down) are retried in place 3 times with exponential backoff; then, and immediately
     * for poison messages, the record is written to dead_letter with its original payload and the reason, and the
     * partition moves on. If even that insert fails the record is redelivered: nothing is dropped.
     */
    @Bean
    CommonErrorHandler kafkaErrorHandler(JdbcClient db) {
        var backoff = new ExponentialBackOffWithMaxRetries(3);
        backoff.setInitialInterval(200);
        backoff.setMultiplier(3);
        backoff.setMaxInterval(2000);
        var handler = new DefaultErrorHandler((record, e) -> {
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            db.sql("INSERT INTO dead_letter (topic, kafka_partition, kafka_offset, msg_key, payload, reason) VALUES (?, ?, ?, ?, ?, ?)"
                            + " ON CONFLICT (topic, kafka_partition, kafka_offset) DO NOTHING")
                    .params(record.topic(), record.partition(), record.offset(), record.key() == null ? null : record.key().toString(),
                            record.value() == null ? null : record.value().toString(),
                            cause.getClass().getSimpleName() + ": " + cause.getMessage()).update();
        }, backoff);
        handler.addNotRetryableExceptions(StatsConsumer.PoisonMessageException.class);
        return handler;
    }

    /** Asks the cluster for its nodes, with a deadline. Not part of readiness: Kafka being down must not stop sales. */
    @Bean(destroyMethod = "close")
    AdminClient kafkaHealthClient(KafkaAdmin admin) {
        var props = new java.util.HashMap<>(admin.getConfigurationProperties());
        props.put("request.timeout.ms", 2000);
        props.put("default.api.timeout.ms", 2000);
        return AdminClient.create(props);
    }

    @Bean
    HealthIndicator kafkaHealthIndicator(AdminClient client) {
        return () -> {
            try {
                int nodes = client.describeCluster().nodes().get(2, TimeUnit.SECONDS).size();
                return Health.up().withDetail("nodes", nodes).build();
            } catch (Exception e) {
                if (e instanceof InterruptedException) {
                    Thread.currentThread().interrupt();
                }
                return Health.down(e).build();
            }
        };
    }
}
