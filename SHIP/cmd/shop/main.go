// shop is the application under migration.
//
//	shop generate --seed 42 --orders 1000000 --out data/raw/orders.ndjson   write a raw feed file (never overwritten)
//	shop ingest FILE...     validate and load feed files; idempotent per file
//	shop profile            print a data profile as markdown
//	shop run                serve traffic as APP_VERSION until stopped, then report
//	shop wait-schema        block until a schema version this APP_VERSION can run on is live
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/krish-codess/codetober-2026/SHIP/internal/shop"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	n, err := strconv.Atoi(env(key, strconv.Itoa(def)))
	if err != nil || n < 0 {
		fmt.Fprintf(os.Stderr, "%s must be a non-negative integer\n", key)
		os.Exit(2)
	}
	return n
}

func main() {
	version := env("APP_VERSION", "v1")
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil)).With("service", "shop", "app_version", version)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if version != "v1" && version != "v2" {
		log.Error("APP_VERSION must be v1 or v2")
		os.Exit(2)
	}
	cmd := "run"
	if len(os.Args) > 1 {
		cmd = os.Args[1]
	}
	if cmd == "generate" {
		if err := generate(os.Args[2:]); err != nil {
			log.Error("generate failed", "err", err)
			os.Exit(1)
		}
		return
	}

	dsn := os.Getenv("APP_DATABASE_URL")
	if dsn == "" {
		log.Error("APP_DATABASE_URL is required; see .env.example")
		os.Exit(2)
	}
	workers := envInt("WORKERS", 8)
	db, err := shop.Open(dsn, "shop", version, workers+2)
	if err != nil {
		log.Error("open", "err", err)
		os.Exit(1)
	}
	defer db.Close()

	switch cmd {
	case "wait-schema":
		err = waitSchema(ctx, db, log)
	case "ingest":
		if err = waitSchema(ctx, db, log); err != nil {
			break
		}
		for _, f := range os.Args[2:] {
			var res shop.IngestResult
			if res, err = shop.Ingest(ctx, db, version, f); err != nil {
				break
			}
			log.Info("ingested", "result", res)
		}
	case "profile":
		err = profile(ctx, db)
	case "run":
		err = run(ctx, db, version, workers, log)
	default:
		err = fmt.Errorf("unknown command %q", cmd)
	}
	if err != nil {
		log.Error(cmd+" failed", "err", err)
		os.Exit(1)
	}
}

func generate(args []string) error {
	fs := flag.NewFlagSet("generate", flag.ExitOnError)
	seed := fs.Uint64("seed", 42, "random seed; the same seed gives the same file")
	orders := fs.Int("orders", 100000, "orders to generate")
	out := fs.String("out", "", "output file (required); refuses to overwrite")
	_ = fs.Parse(args)
	if *out == "" || *orders < 1 || *orders > 50_000_000 {
		return errors.New("usage: shop generate --out FILE [--orders 1..50000000] [--seed N]")
	}
	// Raw inputs are immutable: create exclusively, write once, leave read-only.
	f, err := os.OpenFile(*out, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o444)
	if errors.Is(err, os.ErrExist) {
		fmt.Fprintf(os.Stderr, "%s already exists; keeping it\n", *out)
		return nil
	}
	if err != nil {
		return err
	}
	// The clock is fixed so a seed reproduces the file byte for byte on any day.
	if err := shop.Generate(f, *seed, *orders, time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)); err != nil {
		return err
	}
	return f.Close()
}

// waitSchema retries with capped backoff until the database is up and a compatible schema version is live.
func waitSchema(ctx context.Context, db *sql.DB, log *slog.Logger) error {
	deadline := time.Now().Add(time.Duration(envInt("WAIT_SCHEMA_TIMEOUT_S", 300)) * time.Second)
	for delay := 500 * time.Millisecond; ; delay = min(delay*2, 5*time.Second) {
		err := db.PingContext(ctx)
		if err == nil {
			return nil
		}
		if time.Now().After(deadline) || ctx.Err() != nil {
			return fmt.Errorf("gave up waiting: %w", err)
		}
		log.Info("waiting for a compatible schema version", "reason", err.Error())
		time.Sleep(delay)
	}
}

func run(ctx context.Context, db *sql.DB, version string, workers int, log *slog.Logger) error {
	if err := waitSchema(ctx, db, log); err != nil {
		return err
	}
	mux := http.NewServeMux()
	mux.Handle("GET /metrics", promhttp.Handler())
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { fmt.Fprintln(w, "ok") })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		c, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		var one int
		if err := db.QueryRowContext(c, `SELECT 1 FROM orders LIMIT 1`).Scan(&one); err != nil && !errors.Is(err, sql.ErrNoRows) {
			http.Error(w, "not ready: "+err.Error(), http.StatusServiceUnavailable)
			return
		}
		fmt.Fprintln(w, "ready")
	})
	srv := &http.Server{Addr: env("SHOP_ADDR", ":9100"), Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = srv.ListenAndServe() }()
	defer srv.Close()

	if d := envInt("DURATION_S", 0); d > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(d)*time.Second)
		defer cancel()
	}
	host, _ := os.Hostname()
	rate, _ := strconv.ParseFloat(env("RATE", "20"), 64)
	log.Info("serving traffic", "workers", workers, "rate_per_worker", rate)
	rep := shop.Run(ctx, db, shop.Config{Version: version, Workers: workers, Rate: rate,
		Seed: uint64(time.Now().UnixNano()), Name: strings.ReplaceAll(host, "-", "") + strconv.Itoa(os.Getpid())})
	out, _ := json.Marshal(rep)
	fmt.Println(string(out)) // the report line: what this process saw, for drills and CI to assert on
	if env("STRICT", "") != "" && (rep.Errors > 0 || rep.Mismatches > 0) {
		return fmt.Errorf("%d errors, %d mismatches", rep.Errors, rep.Mismatches)
	}
	return nil
}

var profileQueries = []struct{ title, sql string }{
	{"Table sizes", `SELECT relname AS "table", n_live_tup AS rows, pg_size_pretty(pg_total_relation_size(relid)) AS size
		FROM pg_stat_user_tables WHERE schemaname = 'public' ORDER BY 1`},
	{"Feed files", `SELECT name, lines, accepted, quarantined, duplicates, late FROM ingest_files ORDER BY ingested_at`},
	{"Quarantine by reason", `SELECT reason, count(*) AS lines, round(100.0 * count(*) / (SELECT sum(lines) FROM ingest_files), 3) AS pct_of_feed,
		left(encode(min(raw), 'escape'), 60) AS example FROM quarantine GROUP BY 1 ORDER BY 2 DESC`},
	{"orders: nulls and cardinality", `SELECT count(*) AS rows, count(*) - count(amount) AS amount_null, count(DISTINCT status) AS statuses,
		count(DISTINCT currency) AS currencies, count(DISTINCT customer_id) AS customers, min(placed_at)::date AS first, max(placed_at)::date AS last FROM orders`},
	{"orders.amount: what the text actually contains", `SELECT CASE
			WHEN amount IS NULL THEN 'NULL'
			WHEN amount = '' THEN 'empty string'
			WHEN amount ~ '^[0-9]+[.][0-9]{2}$' THEN 'plain 12.50'
			WHEN amount ~ '^[0-9]+([.][0-9])?$' THEN 'short 12.5 / 12'
			WHEN amount ~ '^[$]' THEN 'dollar sign $12.50'
			WHEN amount ~ '^[0-9]+,[0-9]{1,2}$' THEN 'decimal comma 12,50'
			WHEN amount ~ '^[0-9]{1,3}(,[0-9]{3})+' THEN 'thousands 1,234.50'
			WHEN amount ~ '^\s|\s$' THEN 'padded with spaces'
			WHEN amount ~ '^-' THEN 'negative'
			ELSE 'not a number' END AS format,
		count(*) AS rows, round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct, min(amount) AS example,
		count(*) FILTER (WHERE amount IS NOT NULL AND public.parse_cents(amount) IS NULL) AS unreadable
		FROM orders GROUP BY 1 ORDER BY 2 DESC`},
	{"orders.amount: length", `SELECT min(length(amount)) AS min, max(length(amount)) AS max, round(avg(length(amount)), 1) AS avg FROM orders`},
	{"Readable amounts, in cents", `SELECT min(c), percentile_disc(0.5) WITHIN GROUP (ORDER BY c) AS p50, percentile_disc(0.99) WITHIN GROUP (ORDER BY c) AS p99, max(c)
		FROM (SELECT public.parse_cents(amount) AS c FROM orders) x WHERE c IS NOT NULL`},
	{"orders.status", `SELECT status, count(*) AS rows, round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct FROM orders GROUP BY 1 ORDER BY 2 DESC`},
	{"Orders per customer (skew)", `SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY n) AS p50, percentile_disc(0.9) WITHIN GROUP (ORDER BY n) AS p90,
		percentile_disc(0.99) WITHIN GROUP (ORDER BY n) AS p99, max(n) FROM (SELECT count(*) AS n FROM orders GROUP BY customer_id) x`},
	{"customers: nulls and encoding", `SELECT count(*) AS rows, count(*) - count(name) AS name_null, count(DISTINCT country) AS countries,
		count(*) FILTER (WHERE name ~ '[^[:ascii:]]') AS non_ascii_names FROM customers`},
}

func profile(ctx context.Context, db *sql.DB) error {
	for _, q := range profileQueries {
		rows, err := db.QueryContext(ctx, q.sql)
		if err != nil {
			return fmt.Errorf("%s: %w", q.title, err)
		}
		cols, _ := rows.Columns()
		fmt.Printf("### %s\n\n| %s |\n|%s\n", q.title, strings.Join(cols, " | "), strings.Repeat("---|", len(cols)))
		for rows.Next() {
			vals := make([]sql.NullString, len(cols))
			ptrs := make([]any, len(cols))
			for i := range vals {
				ptrs[i] = &vals[i]
			}
			if err := rows.Scan(ptrs...); err != nil {
				return err
			}
			cells := make([]string, len(cols))
			for i, v := range vals {
				cells[i] = "`" + strings.ReplaceAll(v.String, "|", "\\|") + "`"
				if !v.Valid {
					cells[i] = "*null*"
				}
			}
			fmt.Printf("| %s |\n", strings.Join(cells, " | "))
		}
		rows.Close()
		fmt.Println()
	}
	return nil
}
