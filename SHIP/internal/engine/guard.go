package engine

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync/atomic"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	outcomes       = promauto.NewCounterVec(prometheus.CounterOpts{Name: "shipd_migrations_total", Help: "Migrations finished, by outcome."}, []string{"outcome"})
	abortsTotal    = promauto.NewCounterVec(prometheus.CounterOpts{Name: "shipd_guard_aborts_total", Help: "Migrations aborted by the guard, by threshold."}, []string{"threshold"})
	gBlocked       = promauto.NewGauge(prometheus.GaugeOpts{Name: "shipd_blocked_sessions", Help: "Application sessions currently blocked by the migration."})
	gMaxWait       = promauto.NewGauge(prometheus.GaugeOpts{Name: "shipd_blocked_max_wait_seconds", Help: "Longest current wait of an application session blocked by the migration."})
	gMigratorWait  = promauto.NewGauge(prometheus.GaugeOpts{Name: "shipd_migrator_lock_wait_seconds", Help: "How long the migration itself is currently waiting for a lock."})
	gRollbacks     = promauto.NewGauge(prometheus.GaugeOpts{Name: "shipd_db_rollbacks_per_second", Help: "Rolled-back transactions per second above the pre-migration baseline."})
	gRowsDone      = promauto.NewGauge(prometheus.GaugeOpts{Name: "shipd_backfill_rows_done", Help: "Rows backfilled by the running migration."})
	gRowsTotal     = promauto.NewGauge(prometheus.GaugeOpts{Name: "shipd_backfill_rows_total", Help: "Estimated rows the running migration must backfill."})
	gInFlight      = promauto.NewGauge(prometheus.GaugeOpts{Name: "shipd_migration_in_flight", Help: "1 while a migration is expanding or contracting."})
	lockWaitsTotal = promauto.NewCounter(prometheus.CounterOpts{Name: "shipd_migrator_lock_wait_seconds_total", Help: "Total time migrations have spent waiting for locks."})
)

// Sample is one guard observation of the database.
type Sample struct {
	At             time.Time `json:"at"`
	Phase          string    `json:"phase" enum:"expand,contract"`
	Blocked        int       `json:"blocked" doc:"Application sessions blocked by the migration"`
	MaxWaitMs      int       `json:"max_wait_ms" doc:"Longest wait among those sessions"`
	MigratorWaitMs int       `json:"migrator_wait_ms" doc:"How long the migration itself has been waiting for a lock"`
	Active         int       `json:"active" doc:"Sessions executing a statement"`
	RollbacksPerS  float64   `json:"rollbacks_per_s" doc:"Rolled-back transactions per second above the pre-migration baseline"`
	RowsDone       int64     `json:"rows_done"`
}

// "Blocked by the migration" means the migration's backend is in pg_blocking_pids, which also covers
// sessions queued behind a migration statement that is itself still waiting for its lock.
const guardSQL = `
WITH mig AS (
  SELECT pid FROM pg_stat_activity WHERE datname = current_database() AND application_name = 'pgroll'),
w AS (
  SELECT a.application_name = 'pgroll' AS is_mig,
         EXISTS (SELECT 1 FROM mig WHERE mig.pid = ANY (pg_blocking_pids(a.pid))) AS by_mig,
         COALESCE(1000 * EXTRACT(epoch FROM clock_timestamp() -
           (SELECT min(l.waitstart) FROM pg_locks l WHERE l.pid = a.pid AND NOT l.granted)), 0) AS wait_ms
  FROM pg_stat_activity a
  WHERE a.datname = current_database() AND a.wait_event_type = 'Lock' AND a.backend_type = 'client backend')
SELECT (SELECT count(*) FROM w WHERE by_mig AND NOT is_mig),
       (SELECT COALESCE(max(wait_ms), 0)::int FROM w WHERE by_mig AND NOT is_mig),
       (SELECT COALESCE(max(wait_ms), 0)::int FROM w WHERE is_mig),
       (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active' AND pid <> pg_backend_pid()),
       (SELECT xact_rollback FROM pg_stat_database WHERE datname = current_database())`

// breach names the threshold a sample crosses, or "" when the migration may continue.
func breach(s Settings, x Sample, lockWaitTotal time.Duration) (threshold, reason string) {
	switch {
	case x.MaxWaitMs > s.MaxLockWaitMs:
		return "max_lock_wait_ms", fmt.Sprintf("an application session was blocked by the migration for %d ms (limit %d ms)", x.MaxWaitMs, s.MaxLockWaitMs)
	case x.Blocked > s.MaxBlocked:
		return "max_blocked", fmt.Sprintf("%d application sessions blocked by the migration (limit %d)", x.Blocked, s.MaxBlocked)
	case lockWaitTotal > time.Duration(s.LockBudgetS)*time.Second:
		return "lock_budget_s", fmt.Sprintf("the migration waited %.0f s for locks (budget %d s); a long-running transaction is probably holding the table", lockWaitTotal.Seconds(), s.LockBudgetS)
	case x.RollbacksPerS > s.MaxRollbacksPerS:
		return "max_rollbacks_per_s", fmt.Sprintf("%.1f rolled-back transactions per second above baseline (limit %.1f)", x.RollbacksPerS, s.MaxRollbacksPerS)
	}
	return "", ""
}

func (e *Engine) rollbackRate(ctx context.Context) float64 {
	var a, b float64
	q := `SELECT xact_rollback FROM pg_stat_database WHERE datname = current_database()`
	if e.db.QueryRowContext(ctx, q).Scan(&a) != nil || sleep(ctx, time.Second) != nil || e.db.QueryRowContext(ctx, q).Scan(&b) != nil {
		return 0
	}
	return max(b-a, 0)
}

// guard samples locks and errors twice a second, records them, tells the backfill when to stand still
// and aborts the migration when a threshold is crossed on two consecutive samples.
func (e *Engine) guard(ctx context.Context, run Run, phase string, baseline float64, abort context.CancelCauseFunc, done *atomic.Int64, pressure *atomic.Bool) {
	// A migration statement waits at most lock_timeout before giving up and retrying, so its waits are short
	// windows. They are counted on a fast tick; the full sample, which is costlier, runs every fifth tick.
	const fast, tick = 100 * time.Millisecond, 500 * time.Millisecond
	gInFlight.Set(1)
	gRowsTotal.Set(float64(run.RowsTotal))
	defer func() {
		for _, g := range []prometheus.Gauge{gInFlight, gBlocked, gMaxWait, gMigratorWait, gRollbacks} {
			g.Set(0)
		}
	}()
	type reading struct {
		at        time.Time
		rollbacks float64
	}
	var window []reading // statistics reach pg_stat_database in bursts about a second apart; a 2 s window smooths them
	var lockWait time.Duration
	strikes := 0
	for n := 0; n == 0 || sleep(ctx, fast) == nil; n++ { // the first sample is immediate, so even an instant migration has one
		var waiting bool
		if e.db.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM pg_stat_activity
			WHERE datname = current_database() AND application_name = 'pgroll' AND wait_event_type = 'Lock')`).Scan(&waiting) == nil && waiting {
			lockWait += fast
			lockWaitsTotal.Add(fast.Seconds())
		}
		if n%int(tick/fast) != 0 {
			continue
		}
		x := Sample{Phase: phase, RowsDone: done.Load()}
		var rollbacks float64
		if err := e.db.QueryRowContext(ctx, guardSQL).Scan(&x.Blocked, &x.MaxWaitMs, &x.MigratorWaitMs, &x.Active, &rollbacks); err != nil {
			if ctx.Err() == nil {
				e.log.Warn("guard sample", "run_id", run.ID, "err", err)
			}
			continue
		}
		now := time.Now()
		window = append(window, reading{now, rollbacks})
		if len(window) > 5 {
			window = window[1:]
		}
		if oldest := window[0]; now.Sub(oldest.at) >= time.Second {
			x.RollbacksPerS = max((rollbacks-oldest.rollbacks)/now.Sub(oldest.at).Seconds()-baseline, 0)
		}
		pressure.Store(x.Blocked > 0)
		gBlocked.Set(float64(x.Blocked))
		gMaxWait.Set(float64(x.MaxWaitMs) / 1000)
		gMigratorWait.Set(float64(x.MigratorWaitMs) / 1000)
		gRollbacks.Set(x.RollbacksPerS)
		gRowsDone.Set(float64(x.RowsDone))
		if _, err := e.db.ExecContext(ctx, `
			INSERT INTO shipd.samples (run_id, phase, blocked, max_wait_ms, migrator_wait_ms, active, rollbacks_per_s, rows_done)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
			run.ID, phase, x.Blocked, x.MaxWaitMs, x.MigratorWaitMs, x.Active, x.RollbacksPerS, x.RowsDone); err != nil && ctx.Err() == nil {
			e.log.Warn("guard record", "run_id", run.ID, "err", err)
		}
		if abort == nil { // contract cannot be undone half way; lock_timeout and the lock probe protect it instead
			continue
		}
		threshold, reason := breach(run.Settings, x, lockWait)
		if threshold == "" {
			strikes = 0
			continue
		}
		if strikes++; strikes >= 2 {
			abortsTotal.WithLabelValues(threshold).Inc()
			abort(errors.New("guard: " + reason))
			return
		}
	}
}

// Samples returns guard samples for a run, oldest first. Pass the `at` of the last one as cursor for the next page.
func (e *Engine) Samples(ctx context.Context, id int64, cursor time.Time, limit int) ([]Sample, error) {
	rows, err := e.db.QueryContext(ctx, `
		SELECT at, phase, blocked, max_wait_ms, migrator_wait_ms, active, rollbacks_per_s, rows_done
		FROM shipd.samples WHERE run_id = $1 AND at > $2 ORDER BY at LIMIT $3`, id, cursor, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Sample{}
	for rows.Next() {
		var x Sample
		if err := rows.Scan(&x.At, &x.Phase, &x.Blocked, &x.MaxWaitMs, &x.MigratorWaitMs, &x.Active, &x.RollbacksPerS, &x.RowsDone); err != nil {
			return nil, err
		}
		out = append(out, x)
	}
	return out, rows.Err()
}

// ---- compatibility matrix ----

// Session is a group of connections from one application version on one schema version.
// Applications identify themselves with application_name = "<app>:<app version>:<schema version>".
type Session struct {
	App           string `json:"app"`
	AppVersion    string `json:"app_version"`
	SchemaVersion string `json:"schema_version"`
	Count         int    `json:"count"`
}

func (s Session) String() string {
	return fmt.Sprintf("%s %s on %s (%d sessions)", s.App, s.AppVersion, s.SchemaVersion, s.Count)
}

func (e *Engine) sessions(ctx context.Context) ([]Session, error) {
	rows, err := e.db.QueryContext(ctx, `
		SELECT application_name, count(*) FROM pg_stat_activity
		WHERE datname = current_database() AND application_name LIKE '%:%:%' GROUP BY 1 ORDER BY 1`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Session{}
	for rows.Next() {
		var name string
		var n int
		if err := rows.Scan(&name, &n); err != nil {
			return nil, err
		}
		if p := strings.SplitN(name, ":", 3); len(p) == 3 {
			out = append(out, Session{App: p[0], AppVersion: p[1], SchemaVersion: p[2], Count: n})
		}
	}
	return out, rows.Err()
}

// incompatible lists connected sessions whose application version is not in the compatible set.
func incompatible(sessions []Session, compatible []string) []string {
	ok := map[string]bool{}
	for _, v := range compatible {
		ok[v] = true
	}
	var out []string
	for _, s := range sessions {
		if !ok[s.AppVersion] {
			out = append(out, s.String())
		}
	}
	return out
}

// stranded lists sessions on `version` whose application version has no other live schema version to fall back to.
func (e *Engine) stranded(ctx context.Context, sessions []Session, version string) ([]string, error) {
	var out []string
	for _, s := range sessions {
		if s.SchemaVersion != version {
			continue
		}
		var other bool
		err := e.db.QueryRowContext(ctx, `
			SELECT EXISTS (SELECT 1 FROM shipd.compat c JOIN shipd.schema_versions v ON v.version = c.schema_version
			               WHERE c.app_version = $1 AND v.live AND v.version <> $2)`, s.AppVersion, version).Scan(&other)
		if err != nil {
			return nil, err
		}
		if !other {
			out = append(out, s.String())
		}
	}
	return out, nil
}

// drain waits until no session matches, or the drain timeout passes, and returns what is left.
func (e *Engine) drain(ctx context.Context, s Settings, match func(Session) bool) ([]string, error) {
	deadline := time.Now().Add(time.Duration(s.DrainTimeoutS) * time.Second)
	for {
		sessions, err := e.sessions(ctx)
		if err != nil {
			return nil, err
		}
		var left []string
		for _, x := range sessions {
			if match(x) {
				left = append(left, x.String())
			}
		}
		if len(left) == 0 || time.Now().After(deadline) {
			return left, nil
		}
		if err := sleep(ctx, 500*time.Millisecond); err != nil {
			return left, err
		}
	}
}

type SchemaVersion struct {
	Version string `json:"version"`
	Live    bool   `json:"live" doc:"Applications may connect to this version now"`
}

type Cell struct {
	AppVersion    string `json:"app_version"`
	SchemaVersion string `json:"schema_version"`
	Compatible    bool   `json:"compatible"`
	Sessions      int    `json:"sessions" doc:"Connections from this application version on this schema version right now"`
}

type Matrix struct {
	SchemaVersions []SchemaVersion `json:"schema_versions"`
	AppVersions    []string        `json:"app_versions"`
	Cells          []Cell          `json:"cells" doc:"One cell per application version and schema version"`
}

// Compat returns the compatibility matrix joined with who is connected right now.
func (e *Engine) Compat(ctx context.Context) (Matrix, error) {
	m := Matrix{SchemaVersions: []SchemaVersion{}, AppVersions: []string{}, Cells: []Cell{}}
	rows, err := e.db.QueryContext(ctx, `SELECT version, live FROM shipd.schema_versions ORDER BY seq`)
	if err != nil {
		return m, err
	}
	defer rows.Close()
	for rows.Next() {
		var v SchemaVersion
		if err := rows.Scan(&v.Version, &v.Live); err != nil {
			return m, err
		}
		m.SchemaVersions = append(m.SchemaVersions, v)
	}
	compat := map[[2]string]bool{}
	apps := map[string]bool{}
	crow, err := e.db.QueryContext(ctx, `SELECT app_version, schema_version FROM shipd.compat`)
	if err != nil {
		return m, err
	}
	defer crow.Close()
	for crow.Next() {
		var a, v string
		if err := crow.Scan(&a, &v); err != nil {
			return m, err
		}
		compat[[2]string{a, v}], apps[a] = true, true
	}
	sessions, err := e.sessions(ctx)
	if err != nil {
		return m, err
	}
	count := map[[2]string]int{}
	for _, s := range sessions {
		count[[2]string{s.AppVersion, s.SchemaVersion}] += s.Count
		apps[s.AppVersion] = true
	}
	for a := range apps {
		m.AppVersions = append(m.AppVersions, a)
	}
	sort.Strings(m.AppVersions)
	for _, a := range m.AppVersions {
		for _, v := range m.SchemaVersions {
			k := [2]string{a, v.Version}
			m.Cells = append(m.Cells, Cell{AppVersion: a, SchemaVersion: v.Version, Compatible: compat[k], Sessions: count[k]})
		}
	}
	return m, nil
}

type CompatCheck struct {
	AppVersion    string `json:"app_version"`
	Allowed       bool   `json:"allowed" doc:"Whether this application version may be deployed now"`
	SchemaVersion string `json:"schema_version,omitempty" doc:"The schema version it would connect to"`
	Reason        string `json:"reason"`
}

// Check answers the deploy-gate question: can this application version run against the database right now?
func (e *Engine) Check(ctx context.Context, appVersion string) (CompatCheck, error) {
	c := CompatCheck{AppVersion: appVersion}
	err := e.db.QueryRowContext(ctx, ResolveSQL, appVersion).Scan(&c.SchemaVersion)
	switch {
	case err == nil:
		c.Allowed, c.Reason = true, "compatible with live schema version "+c.SchemaVersion
	case errors.Is(err, sql.ErrNoRows):
		c.Reason, err = "no live schema version is compatible with application version "+appVersion, nil
	}
	return c, err
}

// ResolveSQL picks the newest live schema version an application version can run on. Applications run it on connect.
const ResolveSQL = `
SELECT v.version FROM shipd.schema_versions v JOIN shipd.compat c ON c.schema_version = v.version
WHERE c.app_version = $1 AND v.live ORDER BY v.seq DESC LIMIT 1`
