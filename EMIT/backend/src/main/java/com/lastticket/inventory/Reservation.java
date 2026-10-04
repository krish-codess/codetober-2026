package com.lastticket.inventory;

import io.swagger.v3.oas.annotations.media.Schema;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.UUID;

public record Reservation(
        UUID id, UUID inventoryId, UUID eventId,
        @Schema(hidden = true) @com.fasterxml.jackson.annotation.JsonIgnore String userId,
        int quantity,
        @Schema(allowableValues = {"HELD", "CONFIRMED", "EXPIRED", "RELEASED"}) String status,
        @Schema(description = "Hard deadline. After this instant the hold cannot be confirmed, whether or not the sweeper has run yet.")
        Instant expiresAt,
        Instant createdAt,
        @Schema(description = "Server clock at response time, so clients can count down without trusting their own clock.")
        Instant serverTime) {

    static final String COLUMNS = "id, inventory_id, event_id, user_id, quantity, status, expires_at, created_at, now() AS server_time";

    static Reservation map(ResultSet rs, int row) throws SQLException {
        return new Reservation(rs.getObject("id", UUID.class), rs.getObject("inventory_id", UUID.class),
                rs.getObject("event_id", UUID.class), rs.getString("user_id"), rs.getInt("quantity"), rs.getString("status"),
                instant(rs, "expires_at"), instant(rs, "created_at"), instant(rs, "server_time"));
    }

    static Instant instant(ResultSet rs, String column) throws SQLException {
        OffsetDateTime t = rs.getObject(column, OffsetDateTime.class);
        return t == null ? null : t.toInstant();
    }
}
