package com.lastticket.intake;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.Locale;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * Turns one raw line of the purchase-attempt feed into a normalised {@link Attempt}, or says exactly why it cannot.
 * Pure. Lenient where the intent is unambiguous ({@code "2"}, {@code " floor "}, three timestamp formats); strict
 * where guessing would spend someone's money ({@code 2.5} tickets, a missing user).
 */
public final class AttemptParser {
    public record Attempt(String attemptId, String userId, UUID eventId, String ticketType, String section,
                          int quantity, Instant clientTs) {}

    /** Why a line was quarantined. {@code getMessage()} is the machine-readable reason code. */
    public static final class Invalid extends Exception {
        Invalid(String reason) { super(reason, null, false, false); }
    }

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Pattern CODE = Pattern.compile("[A-Z0-9_]{1,32}");
    private static final Pattern INTEGER = Pattern.compile("-?\\d{1,9}");
    private static final DateTimeFormatter LOCAL = DateTimeFormatter.ofPattern("dd/MM/uuuu HH:mm:ss");

    private AttemptParser() {}

    public static Attempt parse(String line) throws Invalid {
        JsonNode n;
        try {
            n = JSON.readTree(line);
        } catch (JsonProcessingException e) {
            throw new Invalid("MALFORMED_JSON");
        }
        if (n == null || !n.isObject()) {
            throw new Invalid("MALFORMED_JSON");
        }
        String attemptId = text(n, "attempt_id", 100);
        String userId = text(n, "user_id", 64);
        UUID eventId;
        try {
            eventId = UUID.fromString(text(n, "event_id", 36));
        } catch (IllegalArgumentException e) {
            throw new Invalid("BAD_EVENT_ID");
        }
        return new Attempt(attemptId, userId, eventId, code(n, "ticket_type"), code(n, "section"), quantity(n.get("quantity")),
                timestamp(n.get("client_ts")));
    }

    private static String text(JsonNode n, String field, int max) throws Invalid {
        JsonNode v = n.get(field);
        if (v == null || v.isNull() || !v.isTextual() || v.asText().isBlank()) {
            throw new Invalid("MISSING_" + field.toUpperCase(Locale.ROOT));
        }
        String s = v.asText().trim();
        if (s.length() > max) {
            throw new Invalid("BAD_" + field.toUpperCase(Locale.ROOT));
        }
        return s;
    }

    private static String code(JsonNode n, String field) throws Invalid {
        String s = text(n, field, 64).toUpperCase(Locale.ROOT);
        if (!CODE.matcher(s).matches()) {
            throw new Invalid("BAD_" + field.toUpperCase(Locale.ROOT));
        }
        return s;
    }

    private static int quantity(JsonNode v) throws Invalid {
        if (v == null || v.isNull()) {
            throw new Invalid("MISSING_QUANTITY");
        }
        int q;
        if (v.isIntegralNumber() && v.canConvertToInt()) {
            q = v.intValue();
        } else if (v.isTextual() && INTEGER.matcher(v.asText().trim()).matches()) {
            q = Integer.parseInt(v.asText().trim());
        } else {
            throw new Invalid("BAD_QUANTITY");
        }
        if (q < 1 || q > 10) {
            throw new Invalid("BAD_QUANTITY");
        }
        return q;
    }

    /** Informational only (server receipt time decides ordering), so an unreadable timestamp is null, not a failure. */
    static Instant timestamp(JsonNode v) {
        if (v == null || v.isNull()) {
            return null;
        }
        if (v.isIntegralNumber()) {
            return Instant.ofEpochMilli(v.longValue());
        }
        String s = v.asText().trim();
        try {
            return Instant.parse(s);
        } catch (DateTimeParseException e) {
            try {
                return LocalDateTime.parse(s, LOCAL).toInstant(ZoneOffset.UTC); // no zone in the source: assume UTC
            } catch (DateTimeParseException e2) {
                return null;
            }
        }
    }
}
