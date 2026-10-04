package com.lastticket.config;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

@Validated
@ConfigurationProperties("lastticket")
public record AppProperties(
        @NotBlank @Size(min = 32, message = "JWT_SECRET must be at least 32 characters") String jwtSecret,
        @NotBlank String adminApiKey,
        @Min(10) int admissionTtlSeconds,
        @Min(50) long sweeperIntervalMs,
        @Min(50) long outboxIntervalMs,
        boolean seedDemo,
        int seedOnSaleInSeconds) {}
