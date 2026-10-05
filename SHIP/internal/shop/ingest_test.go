package shop_test

import (
	"bytes"
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/krish-codess/codetober-2026/SHIP/internal/shop"
	"github.com/krish-codess/codetober-2026/SHIP/internal/testdb"
)

func TestIngest(t *testing.T) {
	d := testdb.New(t)
	eng := d.Engine(t)
	ctx := context.Background()
	res := d.Seed(t, eng, 5000) // seed 42, so every count below is exact and reproducible
	admin, err := sql.Open("postgres", d.Admin)
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	count := func(q string, args ...any) (n int) {
		t.Helper()
		if err := admin.QueryRow(q, args...).Scan(&n); err != nil {
			t.Fatal(err)
		}
		return n
	}

	if res.Lines != res.Accepted+res.Quarantined+res.Duplicates {
		t.Errorf("lines do not add up: %+v", res)
	}
	if res.Quarantined == 0 || res.Duplicates == 0 || res.Late == 0 {
		t.Errorf("the seeded feed should exercise quarantine, duplicates and late arrivals: %+v", res)
	}
	if n := count(`SELECT count(*) FROM public.orders`); n != res.Accepted {
		t.Errorf("%d orders stored, %d accepted", n, res.Accepted)
	}
	if n := count(`SELECT count(*) FROM public.quarantine`); n != res.Quarantined {
		t.Errorf("%d lines quarantined, %d reported", n, res.Quarantined)
	}
	if n := count(`SELECT count(*) FROM (SELECT lower(btrim(email)) FROM public.customers GROUP BY 1 HAVING count(*) > 1) x`); n != 0 {
		t.Errorf("%d customers stored twice under different spellings of one email", n)
	}

	// Raw input is preserved byte for byte: every quarantined line can be found in the file it came from.
	path := filepath.Join(t.TempDir(), "feed.ndjson")
	var feed bytes.Buffer
	if err := shop.Generate(&feed, 42, 5000, time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, feed.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	lines := bytes.Split(feed.Bytes(), []byte("\n"))
	rows, err := admin.Query(`SELECT line_no, raw FROM public.quarantine`)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var n int
		var raw []byte
		if err := rows.Scan(&n, &raw); err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(raw, lines[n-1]) {
			t.Errorf("quarantined line %d differs from the file", n)
		}
	}
	rows.Close()

	// Idempotent: the same bytes again change nothing, whatever the file is called.
	app := d.Shop(t, "v1", 2)
	again, err := shop.Ingest(ctx, app, "v1", path)
	if err != nil {
		t.Fatal(err)
	}
	if !again.Skipped || again.Accepted != res.Accepted || count(`SELECT count(*) FROM public.orders`) != res.Accepted || count(`SELECT count(*) FROM public.ingest_files`) != 1 {
		t.Errorf("second ingest was not a no-op: %+v", again)
	}

	// A different file overlapping the first: only the new orders land.
	var bigger bytes.Buffer
	if err := shop.Generate(&bigger, 42, 6000, time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)); err != nil {
		t.Fatal(err)
	}
	path2 := filepath.Join(t.TempDir(), "feed2.ndjson")
	if err := os.WriteFile(path2, bigger.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	before := count(`SELECT count(*) FROM public.orders`)
	second, err := shop.Ingest(ctx, app, "v1", path2)
	if err != nil {
		t.Fatal(err)
	}
	if n := count(`SELECT count(*) FROM public.orders`); n != before+second.Accepted || n != count(`SELECT count(DISTINCT order_ref) FROM public.orders`) {
		t.Errorf("overlapping file: %d orders after, %d before, %d accepted", n, before, second.Accepted)
	}

	// The Go and SQL definitions of "what does this amount mean" agree on every value in the table.
	amounts, err := admin.Query(`SELECT DISTINCT amount, public.parse_cents(amount) FROM public.orders WHERE amount IS NOT NULL`)
	if err != nil {
		t.Fatal(err)
	}
	defer amounts.Close()
	checked := 0
	for amounts.Next() {
		var text string
		var fromSQL sql.NullInt64
		if err := amounts.Scan(&text, &fromSQL); err != nil {
			t.Fatal(err)
		}
		fromGo, ok := shop.ParseCents(text)
		if ok != fromSQL.Valid || fromGo != fromSQL.Int64 {
			t.Errorf("amount %q: Go says %d (%v), SQL says %d (%v)", text, fromGo, ok, fromSQL.Int64, fromSQL.Valid)
		}
		checked++
	}
	if checked < 1000 {
		t.Errorf("only %d distinct amounts compared", checked)
	}

	// Least privilege: the application role can write orders and nothing else of consequence.
	for _, stmt := range []string{`DROP TABLE public.orders`, `ALTER TABLE public.orders ADD COLUMN x int`, `DELETE FROM shipd.runs`, `UPDATE shipd.compat SET app_version = 'v9'`, `CREATE SCHEMA mine`} {
		if _, err := app.ExecContext(ctx, stmt); err == nil {
			t.Errorf("the application role was allowed to run: %s", stmt)
		}
	}
}

func TestOpenRefusesWithoutCompatibleSchema(t *testing.T) {
	d := testdb.New(t)
	eng := d.Engine(t)
	d.Seed(t, eng, 100)
	v2 := d.Shop(t, "v2", 1)
	if err := v2.Ping(); err == nil {
		t.Fatal("v2 connected although no schema version it can run on exists")
	}
}
