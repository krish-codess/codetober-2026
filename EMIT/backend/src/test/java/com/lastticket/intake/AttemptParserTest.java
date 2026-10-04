package com.lastticket.intake;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.lastticket.intake.AttemptParser.Attempt;
import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

class AttemptParserTest {
    private static final String EVENT = "00000000-0000-4000-8000-000000000001";

    private static String line(String quantity, String section, String ts) {
        return "{\"attempt_id\":\"a-1\",\"user_id\":\"u-1\",\"event_id\":\"" + EVENT + "\",\"ticket_type\":\"GA\",\"section\":" + section
                + ",\"quantity\":" + quantity + ",\"client_ts\":" + ts + "}";
    }

    @Test
    void cleanLine() throws Exception {
        Attempt a = AttemptParser.parse(line("2", "\"FLOOR\"", "\"2026-10-10T10:00:00.123Z\""));
        assertThat(a).isEqualTo(new Attempt("a-1", "u-1", java.util.UUID.fromString(EVENT), "GA", "FLOOR", 2, Instant.parse("2026-10-10T10:00:00.123Z")));
    }

    @Test
    void unambiguousMessIsNormalised() throws Exception {
        Attempt a = AttemptParser.parse(line("\"3\"", "\" floor \"", "1791626400123"));
        assertThat(a.quantity()).isEqualTo(3);
        assertThat(a.section()).isEqualTo("FLOOR");
        assertThat(a.clientTs()).isEqualTo(Instant.ofEpochMilli(1791626400123L));
    }

    @Test
    void zonelessTimestampIsReadAsUtcAndGarbageTimestampIsNotFatal() throws Exception {
        assertThat(AttemptParser.parse(line("1", "\"FLOOR\"", "\"10/10/2026 10:00:03\"")).clientTs()).isEqualTo(Instant.parse("2026-10-10T10:00:03Z"));
        assertThat(AttemptParser.parse(line("1", "\"FLOOR\"", "\"yesterday\"")).clientTs()).isNull();
        assertThat(AttemptParser.parse(line("1", "\"FLOOR\"", "null")).clientTs()).isNull();
    }

    @ParameterizedTest
    @CsvSource(delimiter = '|', value = {
            "0|\"FLOOR\"|BAD_QUANTITY",
            "-1|\"FLOOR\"|BAD_QUANTITY",
            "99|\"FLOOR\"|BAD_QUANTITY",
            "2.5|\"FLOOR\"|BAD_QUANTITY",
            "\"two\"|\"FLOOR\"|BAD_QUANTITY",
            "true|\"FLOOR\"|BAD_QUANTITY",
            "null|\"FLOOR\"|MISSING_QUANTITY",
            "1|null|MISSING_SECTION",
            "1|\"\"|MISSING_SECTION",
            "1|\"FLOOR; DROP TABLE\"|BAD_SECTION",
            "1|42|MISSING_SECTION",
    })
    void ambiguousOrDangerousValuesAreQuarantinedWithAReason(String quantity, String section, String reason) {
        assertThatThrownBy(() -> AttemptParser.parse(line(quantity, section, "null"))).isInstanceOf(AttemptParser.Invalid.class).hasMessage(reason);
    }

    @Test
    void structuralDefects() {
        assertThatThrownBy(() -> AttemptParser.parse("{\"attempt_id\":\"a-1\",\"user_")).hasMessage("MALFORMED_JSON");
        assertThatThrownBy(() -> AttemptParser.parse("[1,2]")).hasMessage("MALFORMED_JSON");
        assertThatThrownBy(() -> AttemptParser.parse("{}")).hasMessage("MISSING_ATTEMPT_ID");
        assertThatThrownBy(() -> AttemptParser.parse("{\"attempt_id\":\"a\",\"user_id\":null}")).hasMessage("MISSING_USER_ID");
        assertThatThrownBy(() -> AttemptParser.parse("{\"attempt_id\":\"a\",\"user_id\":\"u\",\"event_id\":\"not-a-uuid\"}")).hasMessage("BAD_EVENT_ID");
    }
}
