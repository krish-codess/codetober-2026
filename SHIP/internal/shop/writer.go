package shop

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"math"
	"math/rand/v2"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/lib/pq"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	opsTotal   = promauto.NewCounterVec(prometheus.CounterOpts{Name: "shop_ops_total", Help: "Application operations by version, kind and result."}, []string{"version", "op", "result"})
	opDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{Name: "shop_op_duration_seconds", Help: "Application operation latency.",
		Buckets: []float64{.001, .0025, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5}}, []string{"version", "op"})
	mismatchTotal = promauto.NewCounterVec(prometheus.CounterOpts{Name: "shop_read_mismatches_total", Help: "Orders read back with an amount other than the one written."}, []string{"version"})
)

// resolveSQL picks the newest live schema version this application version can run on.
// The controller owns these two tables; this query is the whole contract between it and the application.
const resolveSQL = `
SELECT v.version FROM shipd.schema_versions v JOIN shipd.compat c ON c.schema_version = v.version
WHERE c.app_version = $1 AND v.live ORDER BY v.seq DESC LIMIT 1`

// ErrNoSchema means the database has no live schema version this application version can run on.
var ErrNoSchema = errors.New("no live schema version is compatible with this application version")

var versionRe = regexp.MustCompile(`^[a-z0-9_]{1,40}$`)

type connector struct {
	driver.Connector
	app, appVersion string
}

// Connect pins every new connection to one schema version and says so in application_name, which is how
// the controller knows who is still on a version before it drops it. Connections are recycled every few
// seconds, so the pool follows the schema forward without a restart.
func (c *connector) Connect(ctx context.Context) (driver.Conn, error) {
	conn, err := c.Connector.Connect(ctx)
	if err != nil {
		return nil, err
	}
	rows, err := conn.(driver.QueryerContext).QueryContext(ctx, resolveSQL, []driver.NamedValue{{Ordinal: 1, Value: c.appVersion}})
	if err != nil {
		conn.Close()
		return nil, err
	}
	dest := make([]driver.Value, 1)
	err = rows.Next(dest)
	rows.Close()
	version := fmt.Sprint(dest[0])
	if b, ok := dest[0].([]byte); ok {
		version = string(b)
	}
	if err != nil || !versionRe.MatchString(version) {
		conn.Close()
		return nil, ErrNoSchema
	}
	// pgroll's triggers compare search_path to the bare schema name, so it must be exactly this and nothing else.
	set := fmt.Sprintf(`SET search_path TO public_%s; SET application_name TO %s`, version, pq.QuoteLiteral(c.app+":"+c.appVersion+":"+version))
	if _, err := conn.(driver.ExecerContext).ExecContext(ctx, set, nil); err != nil {
		conn.Close()
		return nil, err
	}
	return conn, nil
}

// Open returns a pool for one application version.
func Open(dsn, app, appVersion string, maxConns int) (*sql.DB, error) {
	base, err := pq.NewConnector(dsn)
	if err != nil {
		return nil, err
	}
	db := sql.OpenDB(&connector{base, app, appVersion})
	db.SetMaxOpenConns(maxConns)
	db.SetMaxIdleConns(maxConns)
	db.SetConnMaxLifetime(5 * time.Second)
	return db, nil
}

// Report is what a set of writers saw. Zero errors and zero mismatches is the claim "nothing broke".
type Report struct {
	Version    string   `json:"version"`
	Inserts    int64    `json:"inserts_acknowledged"`
	Updates    int64    `json:"updates"`
	Reads      int64    `json:"reads"`
	Checked    int64    `json:"rows_checked"`
	Unreadable int64    `json:"unreadable_amounts_written"` // v1 only: legacy garbage, expected to be quarantined
	Errors     int64    `json:"errors"`
	Retries    int64    `json:"retries"`
	Mismatches int64    `json:"mismatches"`
	P50Ms      float64  `json:"p50_ms"`
	P99Ms      float64  `json:"p99_ms"`
	MaxMs      float64  `json:"max_ms"`
	Samples    []string `json:"first_errors,omitempty"`
	lat        []time.Duration
}

type Config struct {
	Version string // v1 writes orders.amount as text, v2 writes orders.amount_cents
	Workers int
	Rate    float64 // operations per second per worker; 0 = as fast as possible
	Seed    uint64
	Name    string // unique per process, so order_refs never collide
}

// Run drives the workload until ctx is cancelled and returns what happened.
func Run(ctx context.Context, db *sql.DB, cfg Config) Report {
	var mu sync.Mutex
	total := Report{Version: cfg.Version}
	var customers int64
	for db.QueryRowContext(ctx, `SELECT max(id) FROM customers`).Scan(&customers) != nil || customers == 0 {
		if sleep(ctx, 500*time.Millisecond) != nil {
			return total
		}
	}
	var since int64 // only rows newer than this are read back, so the check never scans the seeded history
	_ = db.QueryRowContext(ctx, `SELECT COALESCE(max(id), 0) FROM orders`).Scan(&since)
	var wg sync.WaitGroup
	for w := 0; w < cfg.Workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r := worker(ctx, db, cfg, w, customers, since)
			mu.Lock()
			defer mu.Unlock()
			total.Inserts += r.Inserts
			total.Updates += r.Updates
			total.Reads += r.Reads
			total.Checked += r.Checked
			total.Unreadable += r.Unreadable
			total.Errors += r.Errors
			total.Retries += r.Retries
			total.Mismatches += r.Mismatches
			total.lat = append(total.lat, r.lat...)
			if len(total.Samples) < 5 {
				total.Samples = append(total.Samples, r.Samples...)
			}
		}()
	}
	wg.Wait()
	if len(total.lat) > 0 {
		slices.Sort(total.lat)
		ms := func(q float64) float64 {
			return float64(total.lat[int(q*float64(len(total.lat)-1))].Microseconds()) / 1000
		}
		total.P50Ms, total.P99Ms, total.MaxMs = ms(0.5), ms(0.99), ms(1)
	}
	return total
}

func worker(ctx context.Context, db *sql.DB, cfg Config, n int, customers, since int64) Report {
	// A statement in flight when the process is told to stop is allowed to finish, so that every write the
	// database committed is also one the writer knows about: the acknowledged count can then be checked exactly.
	stmt := context.WithoutCancel(ctx)
	r := rand.New(rand.NewPCG(cfg.Seed, uint64(n)))
	rep := Report{}
	v1 := cfg.Version == "v1"
	col := "amount_cents"
	if v1 {
		col = "amount"
	}
	var mine []int64 // ids this worker inserted, to update later
	seq := 0

	do := func(op string, f func() error) {
		start := time.Now()
		err := retry(ctx, &rep, f)
		d := time.Since(start)
		rep.lat = append(rep.lat, d)
		opDuration.WithLabelValues(cfg.Version, op).Observe(d.Seconds())
		if err != nil {
			rep.Errors++
			opsTotal.WithLabelValues(cfg.Version, op, "error").Inc()
			if len(rep.Samples) < 3 {
				rep.Samples = append(rep.Samples, op+": "+err.Error())
			}
			return
		}
		opsTotal.WithLabelValues(cfg.Version, op, "ok").Inc()
	}

	for ctx.Err() == nil {
		switch x := r.Float64(); {
		case x < 0.55 || len(mine) == 0:
			seq++
			cents := int64(math.Exp(r.NormFloat64()+3.3) * 100)
			// The order_ref carries the amount, so any reader of either version can check what it reads.
			ref := fmt.Sprintf("w-%s-%d-%d-%d", cfg.Name, n, seq, cents)
			var amount any = cents
			if v1 {
				switch y := r.Float64(); {
				case y < 0.06:
					amount = "$" + FormatCents(cents)
				case y < 0.09:
					amount = strings.Replace(FormatCents(cents), ".", ",", 1)
				case y < 0.10: // the legacy application never validated this field
					amount, ref = "N/A", fmt.Sprintf("w-%s-%d-%d-x", cfg.Name, n, seq)
				default:
					amount = FormatCents(cents)
				}
			}
			var id int64
			do("insert", func() error {
				err := db.QueryRowContext(stmt, `INSERT INTO orders (order_ref, customer_id, status, `+col+`, currency, placed_at)
					VALUES ($1, $2, 'pending', $3, 'USD', now()) RETURNING id`, ref, 1+r.Int64N(customers), amount).Scan(&id)
				var pqErr *pq.Error
				if errors.As(err, &pqErr) && pqErr.Code == "23505" { // a retry of an insert that had in fact committed
					return db.QueryRowContext(stmt, `SELECT id FROM orders WHERE order_ref = $1`, ref).Scan(&id)
				}
				return err
			})
			if id != 0 {
				rep.Inserts++
				if amount == "N/A" {
					rep.Unreadable++
				}
				if mine = append(mine, id); len(mine) > 500 {
					mine = mine[250:]
				}
			}
		case x < 0.75:
			id := mine[r.IntN(len(mine))]
			do("update", func() error {
				_, err := db.ExecContext(stmt, `UPDATE orders SET status = $1 WHERE id = $2`, []string{"paid", "shipped", "cancelled"}[r.IntN(3)], id)
				return err
			})
			rep.Updates++
		case x < 0.90: // read back the newest orders from every writer, of either version, and check them
			do("check", func() error {
				rows, err := db.QueryContext(stmt, `SELECT order_ref, `+col+`::text FROM orders WHERE id > $1 AND order_ref LIKE 'w-%' ORDER BY id DESC LIMIT 20`, since)
				if err != nil {
					return err
				}
				defer rows.Close()
				for rows.Next() {
					var ref string
					var got sql.NullString
					if err := rows.Scan(&ref, &got); err != nil {
						return err
					}
					rep.Checked++
					if !agrees(ref, got, v1) {
						rep.Mismatches++
						mismatchTotal.WithLabelValues(cfg.Version).Inc()
						if len(rep.Samples) < 3 {
							rep.Samples = append(rep.Samples, fmt.Sprintf("mismatch: %s read as %q", ref, got.String))
						}
					}
				}
				return rows.Err()
			})
			rep.Reads++
		default: // a customer's recent orders: the read path idx_orders_customer_placed exists for
			do("history", func() error {
				rows, err := db.QueryContext(stmt, `SELECT id, status, `+col+`::text, placed_at FROM orders WHERE customer_id = $1 ORDER BY placed_at DESC LIMIT 10`, 1+r.Int64N(customers))
				if err != nil {
					return err
				}
				defer rows.Close()
				for rows.Next() {
				}
				return rows.Err()
			})
			rep.Reads++
		}
		if cfg.Rate > 0 {
			_ = sleep(ctx, time.Duration(float64(time.Second)/cfg.Rate*(0.5+r.Float64())))
		}
	}
	return rep
}

// agrees checks an amount read through one schema version against the amount encoded in the order_ref.
func agrees(ref string, got sql.NullString, v1 bool) bool {
	want := ref[strings.LastIndexByte(ref, '-')+1:]
	if want == "x" { // written as garbage by v1: v1 reads the garbage back, v2 reads NULL
		return v1 && got.String == "N/A" || !v1 && !got.Valid
	}
	cents, _ := strconv.ParseInt(want, 10, 64)
	if !got.Valid {
		return false
	}
	if v1 {
		c, ok := ParseCents(got.String)
		return ok && c == cents
	}
	return got.String == want
}

// retry runs f, retrying transient failures up to three times with exponential backoff.
func retry(ctx context.Context, rep *Report, f func() error) error {
	var err error
	for attempt := 0; ; attempt++ {
		if err = f(); err == nil || attempt == 3 || !transient(err) {
			return err
		}
		rep.Retries++
		if sleep(ctx, 50*time.Millisecond<<attempt) != nil {
			return err
		}
	}
}

func transient(err error) bool {
	var pqErr *pq.Error
	if errors.As(err, &pqErr) {
		switch pqErr.Code {
		case "40001", "40P01", "57P01", "57P03", "53300": // serialization, deadlock, shutdown, starting up, too many connections
			return true
		}
		return pqErr.Code.Class() == "08"
	}
	return errors.Is(err, driver.ErrBadConn) || errors.Is(err, ErrNoSchema) || strings.Contains(err.Error(), "connection")
}

func sleep(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
