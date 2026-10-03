package com.lastticket;

import java.util.Map;
import org.flywaydb.core.Flyway;

/**
 * Runs the schema migrations and exits. Used as the Kubernetes init container so that the long-running application
 * container never holds the schema owner's credentials (it starts with SPRING_FLYWAY_ENABLED=false and the DML-only
 * role). Flyway takes a database lock, so several pods starting at once migrate exactly once.
 *
 * <pre>java -cp last-ticket.jar com.lastticket.Migrate</pre>
 */
public final class Migrate {
    private Migrate() {}

    public static void main(String[] args) {
        var result = Flyway.configure()
                .dataSource(env("DB_URL"), env("DB_OWNER_USER"), env("DB_OWNER_PASSWORD"))
                .placeholders(Map.of("app_user", env("DB_APP_USER"), "app_password", env("DB_APP_PASSWORD")))
                .load().migrate();
        System.out.println("migrations applied: " + result.migrationsExecuted + ", schema now at " + result.targetSchemaVersion);
    }

    private static String env(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("missing environment variable " + name);
        }
        return value;
    }
}
