// Package testdb gives each integration test its own real PostgreSQL database, initialised
// exactly as production is: roles, grants, pgroll state, migration 01, a seeded feed.
package testdb

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/lib/pq"

	"github.com/krish-codess/codetober-2026/SHIP/internal/engine"
	"github.com/krish-codess/codetober-2026/SHIP/internal/shop"
)

type DB struct {
	Admin, Migrator, App string // connection URLs
	Log                  *slog.Logger
}

// Root is the repository's SHIP directory.
func Root() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(file), "..", "..")
}

// New creates a fresh database. Tests are skipped without TEST_ADMIN_DATABASE_URL, unless REQUIRE_DB is set (CI),
// in which case a missing database is a failure rather than a silent pass.
func New(t *testing.T) *DB {
	t.Helper()
	admin := os.Getenv("TEST_ADMIN_DATABASE_URL")
	if admin == "" {
		if os.Getenv("REQUIRE_DB") != "" {
			t.Fatal("TEST_ADMIN_DATABASE_URL is not set")
		}
		t.Skip("TEST_ADMIN_DATABASE_URL is not set; skipping integration test")
	}
	ctx := context.Background()
	root, err := sql.Open("postgres", admin)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	name := fmt.Sprintf("ship_test_%d", time.Now().UnixNano())
	if _, err := root.ExecContext(ctx, `CREATE DATABASE `+pq.QuoteIdentifier(name)); err != nil {
		t.Fatal(err)
	}
	at := func(user, pass string) string {
		u, _ := url.Parse(admin)
		u.Path = "/" + name
		if user != "" {
			u.User = url.UserPassword(user, pass)
		}
		return u.String()
	}
	out := io.Discard
	if testing.Verbose() {
		out = os.Stderr
	}
	db := &DB{Admin: at("", ""), Migrator: at("ship_test_migrator", "test-migrator"), App: at("shop_test_app", "test-app"),
		Log: slog.New(slog.NewJSONHandler(out, nil))}
	if err := engine.Init(ctx, db.Admin, db.Migrator, db.App); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		root, err := sql.Open("postgres", admin)
		if err != nil {
			return
		}
		defer root.Close()
		_, _ = root.Exec(`DROP DATABASE IF EXISTS ` + pq.QuoteIdentifier(name) + ` WITH (FORCE)`)
	})
	return db
}

// Engine starts a controller on the database and stops it when the test ends.
func (d *DB) Engine(t *testing.T) *engine.Engine {
	t.Helper()
	eng, err := engine.New(context.Background(), d.Migrator, "shop_test_app", d.Log)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(eng.Close)
	return eng
}

// Migration reads migrations/<name>.json.
func Migration(t *testing.T, name string) engine.Submit {
	t.Helper()
	var in engine.Submit
	raw, err := os.ReadFile(filepath.Join(Root(), "migrations", name+".json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &in); err != nil {
		t.Fatal(err)
	}
	return in
}

// Apply submits a migration and waits until it is expanded; with complete it also contracts it.
func Apply(t *testing.T, eng *engine.Engine, in engine.Submit, complete bool) engine.Run {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	run, _, err := eng.Submit(ctx, in, "test")
	if err != nil {
		t.Fatalf("submit %s: %v", in.Name, Describe(err))
	}
	run, err = eng.Wait(ctx, run.ID, engine.Expanded)
	if err != nil || run.State != engine.Expanded {
		t.Fatalf("%s did not expand: state %s, reason %q, err %v", in.Name, run.State, run.Reason, err)
	}
	if !complete {
		return run
	}
	return Complete(t, eng, run.ID)
}

// Complete contracts a run and waits for it.
func Complete(t *testing.T, eng *engine.Engine, id int64) engine.Run {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	for { // the verification that follows the backfill may still be running
		_, err := eng.Complete(ctx, id)
		if err == nil {
			break
		}
		if !strings.Contains(err.Error(), "not finished") || ctx.Err() != nil {
			t.Fatalf("complete: %v", Describe(err))
		}
		time.Sleep(50 * time.Millisecond)
	}
	run, err := eng.Wait(ctx, id, engine.Expanded)
	if err != nil || run.State != engine.Completed {
		t.Fatalf("run %d did not complete: state %s, reason %q, err %v", id, run.State, run.Reason, err)
	}
	return run
}

// Describe includes an engine error's details, which carry the useful part.
func Describe(err error) string {
	if e, ok := err.(*engine.Error); ok {
		return fmt.Sprintf("%s %v", e.Msg, e.Details)
	}
	return fmt.Sprint(err)
}

// Seed applies migration 01 and loads `orders` generated feed lines with a fixed seed.
func (d *DB) Seed(t *testing.T, eng *engine.Engine, orders int) shop.IngestResult {
	t.Helper()
	Apply(t, eng, Migration(t, "01_initial"), true)
	path := filepath.Join(t.TempDir(), "feed.ndjson")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := shop.Generate(f, 42, orders, time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)); err != nil {
		t.Fatal(err)
	}
	f.Close()
	app := d.Shop(t, "v1", 2)
	res, err := shop.Ingest(context.Background(), app, "v1", path)
	app.Close() // a lingering v1 session would, rightly, block the contract of the next migration
	if err != nil {
		t.Fatal(err)
	}
	return res
}

// Shop opens an application pool for one application version.
func (d *DB) Shop(t *testing.T, version string, conns int) *sql.DB {
	t.Helper()
	db, err := shop.Open(d.App, "shop", version, conns)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

// Snapshot describes everything a migration may change in the public schema: columns, constraints,
// indexes, triggers, functions and schemas. Equal snapshots before and after mean a revert left nothing behind.
func (d *DB) Snapshot(t *testing.T) string {
	t.Helper()
	db, err := sql.Open("postgres", d.Admin)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var s sql.NullString
	err = db.QueryRow(`
		SELECT string_agg(line, E'\n' ORDER BY line) FROM (
		  SELECT 'column ' || c.relname || '.' || a.attname || ' ' || format_type(a.atttypid, a.atttypmod) || ' notnull=' || a.attnotnull AS line
		  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
		  WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
		  UNION ALL SELECT 'constraint ' || conrelid::regclass || ' ' || conname || ' ' || pg_get_constraintdef(oid) || ' valid=' || convalidated
		  FROM pg_constraint WHERE connamespace = 'public'::regnamespace
		  UNION ALL SELECT 'index ' || indexdef FROM pg_indexes WHERE schemaname = 'public'
		  UNION ALL SELECT 'trigger ' || tgrelid::regclass || ' ' || tgname FROM pg_trigger WHERE NOT tgisinternal
		  UNION ALL SELECT 'function ' || proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace
		  UNION ALL SELECT 'schema ' || nspname FROM pg_namespace WHERE nspname LIKE 'public%'
		) x`).Scan(&s)
	if err != nil {
		t.Fatal(err)
	}
	return s.String
}
