package engine

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"time"

	"github.com/lib/pq"
	"github.com/xataio/pgroll/pkg/state"
)

// Init runs once as a superuser. pgroll's state needs event triggers, which only a superuser can create;
// everything after this runs as the two unprivileged roles created here. Safe to run again.
func Init(ctx context.Context, adminURL, migratorURL, appURL string) error {
	db, err := sql.Open("postgres", adminURL)
	if err != nil {
		return err
	}
	defer db.Close()
	// The database may still be starting: retry with capped backoff for a minute, then give up loudly.
	for delay, deadline := 250*time.Millisecond, time.Now().Add(time.Minute); ; delay = min(delay*2, 4*time.Second) {
		if err = db.PingContext(ctx); err == nil {
			break
		}
		if time.Now().After(deadline) || ctx.Err() != nil {
			return fmt.Errorf("database not reachable: %w", err)
		}
		time.Sleep(delay)
	}
	migrator, err := url.Parse(migratorURL)
	if err != nil {
		return err
	}
	app, err := url.Parse(appURL)
	if err != nil {
		return err
	}
	for _, u := range []*url.URL{migrator, app} {
		pw, _ := u.User.Password()
		// Role DDL cannot take bind parameters; format() quotes the identifier and the literal server-side.
		var ddl string
		if err := db.QueryRowContext(ctx, `
			SELECT format(CASE WHEN EXISTS (SELECT FROM pg_roles WHERE rolname = $1) THEN 'ALTER' ELSE 'CREATE' END || ' ROLE %I LOGIN PASSWORD %L', $1::text, $2::text)`,
			u.User.Username(), pw).Scan(&ddl); err != nil {
			return err
		}
		if _, err := db.ExecContext(ctx, ddl); err != nil {
			return fmt.Errorf("role %s: %w", u.User.Username(), err)
		}
	}
	st, err := state.New(ctx, adminURL, pgrollState)
	if err != nil {
		return err
	}
	defer st.Close()
	if err := st.Init(ctx); err != nil {
		return fmt.Errorf("pgroll init: %w", err)
	}
	m, a := pq.QuoteIdentifier(migrator.User.Username()), pq.QuoteIdentifier(app.User.Username())
	var dbName string
	if err := db.QueryRowContext(ctx, `SELECT quote_ident(current_database())`).Scan(&dbName); err != nil {
		return err
	}
	grants := fmt.Sprintf(`
		GRANT pg_read_all_stats TO %[1]s;                       -- see other sessions' lock waits, nothing more
		GRANT CREATE ON DATABASE %[3]s TO %[1]s;                -- version schemas
		GRANT ALL ON SCHEMA public TO %[1]s;
		GRANT USAGE, CREATE ON SCHEMA pgroll TO %[1]s;
		GRANT ALL ON ALL TABLES IN SCHEMA pgroll TO %[1]s;
		GRANT USAGE ON SCHEMA public TO %[2]s;
		ALTER DEFAULT PRIVILEGES FOR ROLE %[1]s GRANT USAGE ON SCHEMAS TO %[2]s;
		ALTER DEFAULT PRIVILEGES FOR ROLE %[1]s GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %[2]s;
		ALTER DEFAULT PRIVILEGES FOR ROLE %[1]s GRANT USAGE ON SEQUENCES TO %[2]s;`, m, a, dbName)
	if _, err := db.ExecContext(ctx, grants); err != nil {
		return fmt.Errorf("grants: %w", err)
	}
	return nil
}
