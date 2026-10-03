package com.lastticket;

import static org.assertj.core.api.Assertions.assertThat;

import com.lastticket.intake.AttemptIntake;
import com.lastticket.intake.AttemptIntake.Summary;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.Statement;
import java.util.Map;
import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.core.io.ClassPathResource;

class IntakeAndMigrationTest extends IntegrationTest {

    @Autowired AttemptIntake intake;

    private static String line(String attemptId, String user, UUID event, String section, String quantity) {
        return "{\"attempt_id\":\"" + attemptId + "\",\"user_id\":\"" + user + "\",\"event_id\":\"" + event + "\",\"ticket_type\":\"GA\",\"section\":\""
                + section + "\",\"quantity\":" + quantity + ",\"client_ts\":\"2026-10-10T10:00:00Z\"}";
    }

    @Test
    void messyBatch_isStoredVerbatim_validLinesReserve_badLinesAreQuarantinedWithReasons_duplicatesDoNotDoubleBook() {
        UUID section = newSection(3);
        UUID event = inventory.eventOf(section);
        String body = String.join("\n",
                line("att-0001", "u-1", event, "FLOOR", "2"),          // 1 held
                line("att-0001", "u-1", event, "FLOOR", "2"),          // 2 duplicate of line 1: same reservation, no second hold
                line("att-0002", "u-2", event, " floor ", "\"1\""),    // 3 held after normalisation
                "",                                                    // 4 blank: ignored
                line("att-0003", "u-3", event, "FLOOR", "2.5"),        // 5 quarantined
                line("att-0004", "u-4", event, "BALCONY", "1"),        // 6 quarantined: no such section
                "{\"attempt_id\":\"att-0005\",\"user_",                // 7 quarantined: truncated
                line("att-0006", "u-6", event, "FLOOR", "1"),          // 8 rejected: sold out
                "");
        UUID batch = UUID.randomUUID();

        Summary s = intake.ingest(batch, body);

        assertThat(s).extracting(Summary::lines, Summary::held, Summary::rejected, Summary::quarantined, Summary::alreadyProcessed)
                .containsExactly(7, 3, 1, 3, 0);
        assertThat(s.reasons()).isEqualTo(Map.of("BAD_QUANTITY", 1, "UNKNOWN_INVENTORY", 1, "MALFORMED_JSON", 1, "SOLD_OUT", 1));
        assertThat(position(section)).as("3 tickets held by 2 reservations, not 5 by 3").extracting("held", "available").containsExactly(3, 0);
        assertThat(db.sql("SELECT payload FROM raw_attempt WHERE batch_id = ? AND line_no = 7").param(batch).query(String.class).single())
                .as("raw input preserved byte for byte").isEqualTo("{\"attempt_id\":\"att-0005\",\"user_");
        assertThat(intake.quarantine(0, 1000).stream().filter(q -> q.batchId().equals(batch))).extracting("lineNo", "reason")
                .containsExactly(org.assertj.core.groups.Tuple.tuple(5, "BAD_QUANTITY"), org.assertj.core.groups.Tuple.tuple(6, "UNKNOWN_INVENTORY"),
                        org.assertj.core.groups.Tuple.tuple(7, "MALFORMED_JSON"));

        // The whole batch arrives again (sender timed out and retried): nothing changes.
        Summary again = intake.ingest(batch, body);
        assertThat(again).extracting(Summary::held, Summary::rejected, Summary::quarantined, Summary::alreadyProcessed).containsExactly(0, 0, 0, 7);
        assertThat(position(section).held()).isEqualTo(3);
    }

    @Test
    void generatedSampleFeed_isConsumedEndToEnd_withoutOversell() throws Exception {
        UUID section = newSection(50);
        UUID event = inventory.eventOf(section);
        // The committed sample targets the demo event's five sections; retarget it at this test's event (FLOOR only exists here).
        String feed = new String(java.nio.file.Files.readAllBytes(java.nio.file.Path.of("..", "data", "sample", "attempts-sample.ndjson")), StandardCharsets.UTF_8)
                .replace("00000000-0000-4000-8000-000000000001", event.toString());

        Summary s = intake.ingest(UUID.randomUUID(), feed);

        assertThat(s.lines()).isEqualTo(319);
        assertThat(s.held() + s.rejected() + s.quarantined()).as("every line has exactly one outcome").isEqualTo(s.lines());
        assertThat(s.quarantined()).isPositive();
        assertThat(s.reasons()).containsKeys("UNKNOWN_INVENTORY", "SOLD_OUT");
        assertThat(position(section)).extracting("total", "sold").containsExactly(50, 0);
        assertThat(position(section).held()).isBetween(47, 50); // sold through, to within one refused multi-ticket request
        assertThat(queries.analytics(event)).extracting("oversoldTickets", "snapshotDrift").containsExactly(0, 0);
    }

    /** Every migration has a rollback script; prove they run and leave a database that migrates cleanly again. */
    @Test
    void migrationsRollBackAndReapply() throws Exception {
        String dbName = "rollback_" + UUID.randomUUID().toString().replace("-", "");
        try (Connection c = DriverManager.getConnection(POSTGRES.getJdbcUrl(), POSTGRES.getUsername(), POSTGRES.getPassword());
             Statement st = c.createStatement()) {
            st.execute("CREATE DATABASE " + dbName);
        }
        String url = POSTGRES.getJdbcUrl().replace("/lastticket", "/" + dbName);
        Flyway flyway = Flyway.configure().dataSource(url, POSTGRES.getUsername(), POSTGRES.getPassword())
                .placeholders(Map.of("app_user", "lastticket_app", "app_password", "app-pw")).load();
        assertThat(flyway.migrate().migrationsExecuted).isEqualTo(3);

        try (Connection c = DriverManager.getConnection(url, POSTGRES.getUsername(), POSTGRES.getPassword()); Statement st = c.createStatement()) {
            for (String script : new String[] {"U3__drop_unused_as_of_index.sql", "U2__schema.sql", "U1__app_role.sql"}) {
                st.execute(new String(new ClassPathResource("db/rollback/" + script).getInputStream().readAllBytes(), StandardCharsets.UTF_8)
                        .replace("${app_user}", "lastticket_app"));
            }
            try (var rs = st.executeQuery("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name <> 'flyway_schema_history'")) {
                rs.next();
                assertThat(rs.getInt(1)).as("no application tables left").isZero();
            }
        }
        assertThat(flyway.migrate().migrationsExecuted).as("clean re-apply after rollback").isEqualTo(3);
    }
}
