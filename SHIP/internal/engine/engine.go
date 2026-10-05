// Package engine drives pgroll migrations through expand, verify and contract,
// watching locks and errors and reverting when they exceed their thresholds.
package engine

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/lib/pq"
	"github.com/xataio/pgroll/pkg/backfill"
	"github.com/xataio/pgroll/pkg/migrations"
	"github.com/xataio/pgroll/pkg/roll"
	"github.com/xataio/pgroll/pkg/state"
)

//go:embed schema.sql
var schemaSQL string

const (
	appSchema   = "public"
	pgrollState = "pgroll"
	leaderKey   = 0x53484950 // "SHIP": one controller may drive migrations at a time
)

const (
	Pending     = "pending"
	Expanding   = "expanding"
	Expanded    = "expanded"
	Contracting = "contracting"
	Completed   = "completed"
	Reverting   = "reverting"
	Reverted    = "reverted"
	Failed      = "failed"
)

// Error is a failure the caller can act on; Kind maps to an HTTP status.
type Error struct {
	Kind    string // not_found | conflict | invalid
	Msg     string
	Details []string
}

func (e *Error) Error() string { return e.Msg }

func conflict(msg string, details ...string) error {
	return &Error{Kind: "conflict", Msg: msg, Details: details}
}

// Settings are the per-migration throttles and abort thresholds.
type Settings struct {
	BatchSize        int     `json:"batch_size,omitempty" minimum:"100" maximum:"50000" doc:"Rows per backfill batch (default 2000)"`
	BatchDelayMs     int     `json:"batch_delay_ms,omitempty" minimum:"0" maximum:"10000" doc:"Minimum pause between batches (default 10)"`
	ThrottleRatio    float64 `json:"throttle_ratio,omitempty" minimum:"0" maximum:"20" doc:"Pause this many times the duration of the last batch, so a slower database gets a slower backfill (default 0.5)"`
	LockTimeoutMs    int     `json:"lock_timeout_ms,omitempty" minimum:"50" maximum:"10000" doc:"lock_timeout for every migration statement; a statement that cannot get its lock gives up and retries (default 1000)"`
	LockBudgetS      int     `json:"lock_budget_s,omitempty" minimum:"1" maximum:"3600" doc:"Total time the migration may spend waiting for locks before it is aborted (default 10)"`
	MaxLockWaitMs    int     `json:"max_lock_wait_ms,omitempty" minimum:"100" maximum:"60000" doc:"Abort if an application session is blocked by the migration for longer than this (default 3000)"`
	MaxBlocked       int     `json:"max_blocked,omitempty" minimum:"1" maximum:"10000" doc:"Abort if more than this many application sessions are blocked by the migration (default 25)"`
	MaxRollbacksPerS float64 `json:"max_rollbacks_per_s,omitempty" minimum:"0.1" maximum:"100000" doc:"Abort if rolled-back transactions per second exceed the pre-migration baseline by more than this (default 20)"`
	DrainTimeoutS    int     `json:"drain_timeout_s,omitempty" minimum:"1" maximum:"3600" doc:"How long contract and abort wait for sessions to leave the schema version being dropped (default 30)"`
}

func (s Settings) WithDefaults() Settings {
	def := func(v *int, d int) {
		if *v == 0 {
			*v = d
		}
	}
	def(&s.BatchSize, 2000)
	def(&s.BatchDelayMs, 10)
	def(&s.LockTimeoutMs, 1000)
	def(&s.LockBudgetS, 10)
	def(&s.MaxLockWaitMs, 3000)
	def(&s.MaxBlocked, 25)
	def(&s.DrainTimeoutS, 30)
	if s.ThrottleRatio == 0 {
		s.ThrottleRatio = 0.5
	}
	if s.MaxRollbacksPerS == 0 {
		s.MaxRollbacksPerS = 20
	}
	return s
}

// Submit is a migration request: pgroll operations plus the application versions that can run on the result.
type Submit struct {
	Name                  string           `json:"name" pattern:"^[a-z0-9_]{1,40}$" doc:"Migration name; also names the schema version (public_<name>)" example:"02_amount_to_cents"`
	CompatibleAppVersions []string         `json:"compatible_app_versions" minItems:"1" maxItems:"50" doc:"Application versions that can run against the schema this migration produces"`
	Operations            []map[string]any `json:"operations" minItems:"1" maxItems:"100" doc:"pgroll operations (https://pgroll.com/docs)"`
	Settings              Settings         `json:"settings,omitempty,omitzero"`
}

type TableStat struct {
	Table string `json:"table"`
	Rows  int64  `json:"rows" doc:"Estimated live rows when the migration started"`
	Bytes int64  `json:"bytes" doc:"Table plus indexes, in bytes, when the migration started"`
}

type Run struct {
	ID                    int64            `json:"id"`
	Name                  string           `json:"name"`
	State                 string           `json:"state" enum:"pending,expanding,expanded,contracting,completed,reverting,reverted,failed"`
	Reason                string           `json:"reason" doc:"Why the run was reverted, failed or postponed; empty otherwise"`
	CorrelationID         string           `json:"correlation_id"`
	CompatibleAppVersions []string         `json:"compatible_app_versions"`
	Operations            []map[string]any `json:"operations"`
	Settings              Settings         `json:"settings"`
	Tables                []TableStat      `json:"tables"`
	RowsTotal             int64            `json:"rows_total" doc:"Estimated rows to backfill"`
	RowsDone              int64            `json:"rows_done"`
	Verification          *Verification    `json:"verification,omitempty"`
	CreatedAt             time.Time        `json:"created_at"`
	StartedAt             *time.Time       `json:"started_at,omitempty"`
	DDLDoneAt             *time.Time       `json:"ddl_done_at,omitempty"`
	ExpandedAt            *time.Time       `json:"expanded_at,omitempty"`
	ContractStartedAt     *time.Time       `json:"contract_started_at,omitempty"`
	FinishedAt            *time.Time       `json:"finished_at,omitempty"`
	UpdatedAt             time.Time        `json:"updated_at"`
}

type Engine struct {
	db     *sql.DB
	url    string
	log    *slog.Logger
	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
	leader *sql.Conn
	lost   chan struct{} // closed if the leader connection, and with it the leader lock, is gone

	mu     sync.Mutex
	aborts map[int64]context.CancelCauseFunc
}

// New connects as the migrator role, bootstraps the control-plane schema, waits to
// become the only active controller and recovers any run a previous process left mid-flight.
func New(ctx context.Context, dbURL, appRole string, log *slog.Logger) (*Engine, error) {
	db, err := sql.Open("postgres", withParam(dbURL, "application_name", "shipd"))
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(8)
	e := &Engine{db: db, url: dbURL, log: log, aborts: map[int64]context.CancelCauseFunc{}, lost: make(chan struct{})}
	e.ctx, e.cancel = context.WithCancel(context.Background())

	if e.leader, err = db.Conn(ctx); err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	for held := false; !held; {
		if err := e.leader.QueryRowContext(ctx, `SELECT pg_try_advisory_lock($1)`, leaderKey).Scan(&held); err != nil {
			return nil, err
		}
		if !held {
			log.Info("another controller holds the leader lock; standing by")
			if err := sleep(ctx, 2*time.Second); err != nil {
				return nil, err
			}
		}
	}
	if _, err := db.ExecContext(ctx, schemaSQL); err != nil {
		return nil, fmt.Errorf("bootstrap schema: %w", err)
	}
	if appRole != "" { // the app reads the matrix to pick its schema version; nothing else
		// Default privileges give the application DML on everything the migrator creates; take that back here.
		grant := fmt.Sprintf(`REVOKE ALL ON ALL TABLES IN SCHEMA shipd FROM %[1]s; REVOKE ALL ON ALL SEQUENCES IN SCHEMA shipd FROM %[1]s;
			GRANT USAGE ON SCHEMA shipd TO %[1]s; GRANT SELECT ON shipd.schema_versions, shipd.compat TO %[1]s`, pq.QuoteIdentifier(appRole))
		if _, err := db.ExecContext(ctx, grant); err != nil {
			return nil, fmt.Errorf("grant to %s: %w", appRole, err)
		}
	}
	e.recover()
	go func() { // without the leader connection there is no leader lock: stop rather than risk two controllers
		for sleep(e.ctx, 5*time.Second) == nil {
			if err := e.leader.PingContext(e.ctx); err != nil && e.ctx.Err() == nil {
				log.Error("leader connection lost; stopping", "err", err)
				close(e.lost)
				e.cancel()
				return
			}
		}
	}()
	return e, nil
}

// Lost is closed when the controller can no longer prove it is the only one; the process should exit.
func (e *Engine) Lost() <-chan struct{} { return e.lost }

// Close stops driving migrations without cleaning up, exactly as a crash would; the next New recovers.
func (e *Engine) Close() {
	e.cancel()
	e.wg.Wait()
	e.leader.Close()
	e.db.Close()
}

// DB is the controller's connection pool (migrator role), for read-only catalog queries.
func (e *Engine) DB() *sql.DB { return e.db }

// Ready reports whether the controller can do its job: database reachable, pgroll initialised, leader lock held.
func (e *Engine) Ready(ctx context.Context) error {
	var ok bool
	if err := e.leader.QueryRowContext(ctx, `SELECT to_regclass('pgroll.migrations') IS NOT NULL`).Scan(&ok); err != nil {
		return fmt.Errorf("database: %w", err)
	}
	if !ok {
		return errors.New("pgroll is not initialised; run `shipd init`")
	}
	return nil
}

func (e *Engine) recover() {
	rows, err := e.list(e.ctx, `WHERE state IN ('pending','expanding','reverting','contracting') ORDER BY id`)
	if err != nil {
		e.log.Error("recover", "err", err)
		return
	}
	for _, run := range rows {
		log := e.runLog(run)
		log.Warn("recovering run left mid-flight", "state", run.State)
		if run.State == Contracting {
			e.spawn(func() { e.contract(run, log) })
			continue
		}
		e.revert(run, "controller restarted while "+run.State+"; changes rolled back", log)
	}
}

func (e *Engine) spawn(f func()) {
	e.wg.Add(1)
	go func() { defer e.wg.Done(); f() }()
}

func (e *Engine) runLog(run Run) *slog.Logger {
	return e.log.With("run_id", run.ID, "migration", run.Name, "correlation_id", run.CorrelationID)
}

// ---- store ----

func (e *Engine) list(ctx context.Context, where string, args ...any) ([]Run, error) {
	rows, err := e.db.QueryContext(ctx, `SELECT to_jsonb(r) FROM shipd.runs r `+where, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	runs := []Run{}
	for rows.Next() {
		var raw []byte
		var run Run
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(raw, &run); err != nil {
			return nil, err
		}
		runs = append(runs, run)
	}
	return runs, rows.Err()
}

func (e *Engine) Get(ctx context.Context, id int64) (Run, error) {
	runs, err := e.list(ctx, `WHERE id = $1`, id)
	if err != nil {
		return Run{}, err
	}
	if len(runs) == 0 {
		return Run{}, &Error{Kind: "not_found", Msg: fmt.Sprintf("migration run %d not found", id)}
	}
	return runs[0], nil
}

// List returns runs newest first. Pass the last id of a page as cursor to get the next one.
func (e *Engine) List(ctx context.Context, cursor int64, limit int) ([]Run, error) {
	if cursor <= 0 {
		cursor = 1<<63 - 1
	}
	return e.list(ctx, `WHERE id < $1 ORDER BY id DESC LIMIT $2`, cursor, limit)
}

func (e *Engine) set(id int64, frag string, args ...any) {
	_, err := e.db.ExecContext(e.ctx, `UPDATE shipd.runs SET `+frag+`, updated_at = now() WHERE id = $1`, append([]any{id}, args...)...)
	if err != nil {
		e.log.Error("update run", "run_id", id, "err", err)
	}
}

// move is a compare-and-set on the run state, so two requests cannot both start the same transition.
func (e *Engine) move(ctx context.Context, id int64, from, to, extra string) (bool, error) {
	res, err := e.db.ExecContext(ctx, `UPDATE shipd.runs SET state = $3, updated_at = now()`+extra+` WHERE id = $1 AND state = $2`, id, from, to)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n == 1, nil
}

// ---- pgroll ----

func withParam(dbURL, k, v string) string {
	u, err := url.Parse(dbURL)
	if err != nil {
		return dbURL
	}
	q := u.Query()
	q.Set(k, v)
	u.RawQuery = q.Encode()
	return u.String()
}

// pgroll opens the migration connection. lock_timeout goes in the connection string so every pooled
// connection has it; pgroll's own option only sets it on the first.
func (e *Engine) pgroll(ctx context.Context, lockTimeoutMs int) (*roll.Roll, error) {
	st, err := state.New(ctx, e.url, pgrollState)
	if err != nil {
		return nil, err
	}
	m, err := roll.New(ctx, withParam(e.url, "lock_timeout", strconv.Itoa(lockTimeoutMs)), appSchema, st)
	if err != nil {
		st.Close()
		return nil, err
	}
	return m, nil
}

func parse(run Run) (*migrations.Migration, error) {
	ops, err := json.Marshal(run.Operations)
	if err != nil {
		return nil, err
	}
	return migrations.ParseMigration(&migrations.RawMigration{Name: run.Name, Operations: ops})
}

func opTables(ops []map[string]any) []string {
	var out []string
	seen := map[string]bool{}
	for _, op := range ops {
		for kind, body := range op {
			b, _ := body.(map[string]any)
			t, _ := b["table"].(string)
			if kind == "create_table" {
				t = "" // nothing to backfill or lock: the table does not exist yet
			}
			if t != "" && !seen[t] {
				seen[t] = true
				out = append(out, t)
			}
		}
	}
	return out
}

func (e *Engine) tableStats(ctx context.Context, tables []string) []TableStat {
	out := []TableStat{}
	for _, t := range tables {
		s := TableStat{Table: t}
		err := e.db.QueryRowContext(ctx, `
			SELECT COALESCE(NULLIF(s.n_live_tup, 0), GREATEST(c.reltuples, 0)::bigint), pg_total_relation_size(c.oid)
			FROM pg_class c LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
			WHERE c.oid = to_regclass($1)`, appSchema+"."+pq.QuoteIdentifier(t)).Scan(&s.Rows, &s.Bytes)
		if err == nil && s.Rows == 0 { // never analysed: count, as pgroll does
			_ = e.db.QueryRowContext(ctx, `SELECT count(*) FROM `+appSchema+`.`+pq.QuoteIdentifier(t)).Scan(&s.Rows)
		}
		if err == nil { // a table the migration itself creates has no row here: nothing to lock or backfill yet
			out = append(out, s)
		}
	}
	return out
}

// ---- submit / expand ----

// Submit registers a migration and starts expanding it. Submitting the same migration again returns the existing run.
func (e *Engine) Submit(ctx context.Context, in Submit, correlationID string) (Run, bool, error) {
	in.Settings = in.Settings.WithDefaults()
	candidate := Run{Name: in.Name, Operations: in.Operations}
	mig, err := parse(candidate)
	if err != nil {
		return Run{}, false, &Error{Kind: "invalid", Msg: "operations are not valid pgroll operations", Details: []string{err.Error()}}
	}

	if existing, err := e.list(ctx, `WHERE name = $1 AND state NOT IN ('reverted','failed')`, in.Name); err != nil {
		return Run{}, false, err
	} else if len(existing) == 1 {
		a, _ := json.Marshal(existing[0].Operations)
		b, _ := json.Marshal(in.Operations)
		if string(a) != string(b) {
			return Run{}, false, conflict(fmt.Sprintf("a different migration named %q already exists (run %d, %s)", in.Name, existing[0].ID, existing[0].State))
		}
		return existing[0], false, nil
	}

	m, err := e.pgroll(ctx, in.Settings.LockTimeoutMs)
	if err != nil {
		return Run{}, false, err
	}
	err = m.Validate(ctx, mig)
	m.Close()
	if err != nil {
		return Run{}, false, &Error{Kind: "invalid", Msg: "migration cannot be applied to the current schema", Details: []string{err.Error()}}
	}

	ops, _ := json.Marshal(in.Operations)
	settings, _ := json.Marshal(in.Settings)
	var id int64
	err = e.db.QueryRowContext(ctx, `
		INSERT INTO shipd.runs (name, operations, compatible_app_versions, settings, correlation_id)
		VALUES ($1, $2, $3, $4, $5) RETURNING id`,
		in.Name, ops, pq.Array(in.CompatibleAppVersions), settings, correlationID).Scan(&id)
	var pqErr *pq.Error
	if errors.As(err, &pqErr) && pqErr.Code == "23505" {
		return Run{}, false, conflict("another migration is in flight; complete or abort it first")
	}
	if err != nil {
		return Run{}, false, err
	}
	run, err := e.Get(ctx, id)
	if err != nil {
		return Run{}, false, err
	}
	e.spawn(func() { e.expand(run) })
	return run, true, nil
}

func (e *Engine) expand(run Run) {
	log := e.runLog(run)
	ctx, abort := context.WithCancelCause(e.ctx)
	defer abort(nil)
	e.mu.Lock()
	e.aborts[run.ID] = abort
	e.mu.Unlock()
	defer func() {
		e.mu.Lock()
		delete(e.aborts, run.ID)
		e.mu.Unlock()
	}()

	err := e.doExpand(ctx, run, abort, log)
	if err == nil || e.ctx.Err() != nil { // done, or shutting down: recover() reverts on the next start
		return
	}
	reason := err.Error()
	if cause := context.Cause(ctx); cause != nil && !errors.Is(cause, context.Canceled) {
		reason = cause.Error()
	}
	e.revert(run, reason, log)
}

func (e *Engine) doExpand(ctx context.Context, run Run, abort context.CancelCauseFunc, log *slog.Logger) error {
	s := run.Settings
	mig, err := parse(run)
	if err != nil {
		return err
	}
	m, err := e.pgroll(ctx, s.LockTimeoutMs)
	if err != nil {
		return err
	}
	defer m.Close()

	tables := e.tableStats(ctx, opTables(run.Operations))
	tablesJSON, _ := json.Marshal(tables)
	baseline := e.rollbackRate(ctx)
	if ok, err := e.move(ctx, run.ID, Pending, Expanding, `, started_at = now()`); err != nil || !ok {
		return fmt.Errorf("run is no longer pending: %v", err)
	}
	e.set(run.ID, `tables = $2`, tablesJSON)
	log.Info("expand started", "tables", tables, "baseline_rollbacks_per_s", baseline)

	// Prove the exclusive lock is obtainable before changing anything. With a long transaction on the table
	// this fails within the lock budget and the run reverts having touched nothing.
	for _, t := range tables {
		if err := e.probeLock(ctx, t.Table, s); err != nil {
			return err
		}
	}

	var done atomic.Int64
	var pressure atomic.Bool
	gctx, stopGuard := context.WithCancel(ctx)
	defer stopGuard()
	e.spawn(func() { e.guard(gctx, run, "expand", baseline, abort, &done, &pressure) })

	job, err := m.StartDDLOperations(ctx, mig)
	if err != nil {
		return err
	}
	e.set(run.ID, `ddl_done_at = now()`)

	// Backfill. pgroll calls back before each batch, which is where the throttle lives.
	var total, base int64
	for _, t := range job.Tables {
		for _, ts := range tables {
			if ts.Table == t.Name {
				total += ts.Rows
			}
		}
	}
	e.set(run.ID, `rows_total = $2`, total)
	last, lastWrite := time.Now(), time.Now()
	cfg := backfill.NewConfig(backfill.WithBatchSize(s.BatchSize))
	cfg.AddCallback(func(n, _ int64) {
		batch := time.Since(last)
		done.Store(base + n)
		if time.Since(lastWrite) > time.Second {
			lastWrite = time.Now()
			e.set(run.ID, `rows_done = LEAST($2, rows_total)`, base+n)
		}
		pause := max(time.Duration(s.BatchDelayMs)*time.Millisecond, time.Duration(float64(batch)*s.ThrottleRatio))
		if n == 0 {
			pause = 0
		}
		for sleep(ctx, pause) == nil && pressure.Load() { // application sessions are queued behind us: stand still
			pause = 250 * time.Millisecond
		}
		last = time.Now()
	})
	bf := backfill.New(m.PgConn(), cfg)
	if err := bf.CreateTriggers(ctx, job); err != nil {
		return err
	}
	for _, t := range job.Tables {
		log.Info("backfill started", "table", t.Name)
		if err := bf.Start(ctx, t); err != nil {
			return fmt.Errorf("backfill %s: %w", t.Name, err)
		}
		base = done.Load()
	}
	stopGuard()
	if ctx.Err() != nil {
		return ctx.Err()
	}

	// Only now may applications connect to the new version: before the backfill finishes, the new columns are incomplete.
	tx, err := e.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.Exec(`INSERT INTO shipd.schema_versions (version, live) VALUES ($1, true) ON CONFLICT (version) DO UPDATE SET live = true`, run.Name); err != nil {
		return err
	}
	if _, err := tx.Exec(`INSERT INTO shipd.compat (app_version, schema_version) SELECT unnest($1::text[]), $2 ON CONFLICT DO NOTHING`, pq.Array(run.CompatibleAppVersions), run.Name); err != nil {
		return err
	}
	if _, err := tx.Exec(`UPDATE shipd.runs SET state = 'expanded', expanded_at = now(), updated_at = now(), rows_done = rows_total WHERE id = $1 AND state = 'expanding'`, run.ID); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	log.Info("expanded: old and new schema versions are both live", "rows", done.Load())

	if v, err := e.verify(e.ctx, run); err != nil {
		log.Error("verification after expand", "err", err)
	} else {
		log.Info("verification", "ok", v.OK, "checks", v.Checks)
	}
	return nil
}

func (e *Engine) revert(run Run, reason string, log *slog.Logger) {
	log.Warn("reverting", "reason", reason)
	e.set(run.ID, `state = 'reverting', reason = $2`, reason)
	ctx, cancel := context.WithTimeout(e.ctx, 10*time.Minute)
	defer cancel()

	err := func() error {
		m, err := e.pgroll(ctx, run.Settings.LockTimeoutMs)
		if err != nil {
			return err
		}
		defer m.Close()
		if active, err := m.State().GetActiveMigration(ctx, appSchema); err == nil && active.Name == run.Name {
			atomic, err := e.rollbackAtomically(ctx, run, m, active)
			if !atomic { // DROP INDEX CONCURRENTLY cannot run in a transaction: let pgroll do it step by step
				err = m.Rollback(ctx)
			}
			if err != nil {
				return err
			}
		}
		_, err = e.db.ExecContext(ctx, `DELETE FROM shipd.schema_versions WHERE version = $1`, run.Name)
		return err
	}()
	if e.ctx.Err() != nil {
		return
	}
	if err != nil {
		log.Error("revert failed", "err", err)
		e.set(run.ID, `state = 'failed', finished_at = now(), reason = $2`, reason+"; revert failed: "+err.Error())
		outcomes.WithLabelValues(Failed).Inc()
		return
	}
	e.set(run.ID, `state = 'reverted', finished_at = now()`)
	outcomes.WithLabelValues(Reverted).Inc()
	log.Info("reverted: schema is as it was before the migration")
}

// ---- contract ----

// Complete runs the gate (representations agree, no connected application depends on the old version)
// and, if it passes, starts the contract phase in the background.
func (e *Engine) Complete(ctx context.Context, id int64) (Run, error) {
	run, err := e.Get(ctx, id)
	if err != nil {
		return Run{}, err
	}
	switch run.State {
	case Completed, Contracting:
		return run, nil
	case Failed: // a contract that died half way can be retried; pgroll's complete steps are IF EXISTS
		if run.ContractStartedAt == nil {
			return Run{}, conflict("this run failed before contracting; submit the migration again")
		}
		if ok, err := e.move(ctx, id, Failed, Contracting, `, finished_at = NULL`); err != nil || !ok {
			return Run{}, conflict("run changed state; retry")
		}
	case Expanded:
		// The scan itself runs in contract(); here the last result is enough to refuse early.
		if run.Verification == nil {
			return Run{}, conflict("verification has not finished yet; retry in a moment")
		}
		if v := run.Verification; !v.OK {
			return Run{}, conflict("old and new representations disagree; refusing to drop the old one", v.problems()...)
		}
		sessions, err := e.sessions(ctx)
		if err != nil {
			return Run{}, err
		}
		if stuck := incompatible(sessions, run.CompatibleAppVersions); len(stuck) > 0 {
			return Run{}, conflict("connected application versions cannot run on schema version "+run.Name+"; stop them first", stuck...)
		}
		if ok, err := e.move(ctx, id, Expanded, Contracting, `, contract_started_at = now(), reason = ''`); err != nil || !ok {
			return Run{}, conflict("run changed state; retry")
		}
	default:
		return Run{}, conflict(fmt.Sprintf("a %s migration cannot be completed", run.State))
	}
	run, err = e.Get(ctx, id)
	if err != nil {
		return Run{}, err
	}
	log := e.runLog(run)
	e.spawn(func() { e.contract(run, log) })
	return run, nil
}

func (e *Engine) contract(run Run, log *slog.Logger) {
	ctx, s := e.ctx, run.Settings
	postpone := func(why string) { // nothing has been dropped yet: step back to expanded
		log.Warn("contract postponed", "reason", why)
		e.set(run.ID, `state = 'expanded', reason = $2`, "contract postponed: "+why)
	}

	// Compatible applications hop to the new version as their connections recycle; wait for the old one to empty.
	if left, err := e.drain(ctx, s, func(x Session) bool { return x.SchemaVersion != run.Name }); err != nil || len(left) > 0 {
		postpone(fmt.Sprintf("sessions still on an older schema version after %ds: %v %v", s.DrainTimeoutS, left, err))
		return
	}
	if v, err := e.verify(ctx, run); err != nil || !v.OK {
		postpone(fmt.Sprintf("verification no longer passes: %v %v", v.problems(), err))
		return
	}

	gctx, stopGuard := context.WithCancel(ctx)
	defer stopGuard()
	var done atomic.Int64
	done.Store(run.RowsDone)
	e.spawn(func() { e.guard(gctx, run, "contract", 0, nil, &done, new(atomic.Bool)) })

	// untouched: the attempt failed without changing the schema, so the run can simply stay expanded.
	untouched, err := func() (bool, error) {
		mig, err := parse(run)
		if err != nil {
			return true, err
		}
		m, err := e.pgroll(ctx, s.LockTimeoutMs)
		if err != nil {
			return true, err
		}
		defer m.Close()
		if active, err := m.State().GetActiveMigration(ctx, appSchema); err != nil || active.Name != run.Name {
			return true, nil // already completed by a previous attempt
		}
		if atomic, err := e.completeAtomically(ctx, run, m, mig); atomic {
			return true, err
		}
		// Not possible in one transaction (raw SQL, DROP INDEX CONCURRENTLY): pgroll runs its steps one by one.
		// Prove the locks are obtainable first, so a long transaction postpones the contract instead of splitting it.
		for _, t := range e.tableStats(ctx, opTables(run.Operations)) {
			if err := e.probeLock(ctx, t.Table, s); err != nil {
				return true, err
			}
		}
		cctx, cancel := context.WithTimeout(ctx, 30*time.Minute)
		defer cancel()
		return false, m.Complete(cctx)
	}()
	if e.ctx.Err() != nil {
		return
	}
	if err != nil && untouched {
		postpone(err.Error())
		return
	}
	if err != nil {
		log.Error("contract failed", "err", err)
		e.set(run.ID, `state = 'failed', finished_at = now(), reason = $2`, "contract failed part way: "+err.Error()+"; retry complete")
		outcomes.WithLabelValues(Failed).Inc()
		return
	}
	if _, err := e.db.ExecContext(ctx, `UPDATE shipd.schema_versions SET live = (version = $1)`, run.Name); err != nil {
		log.Error("retire old versions", "err", err)
	}
	e.set(run.ID, `state = 'completed', finished_at = now(), reason = ''`)
	outcomes.WithLabelValues(Completed).Inc()
	log.Info("completed: old representation dropped")
}

// ---- abort ----

// Abort reverts a migration that has not been contracted. It is safe to call repeatedly.
func (e *Engine) Abort(ctx context.Context, id int64, why string) (Run, error) {
	run, err := e.Get(ctx, id)
	if err != nil {
		return Run{}, err
	}
	reason := "aborted by operator"
	if why != "" {
		reason += ": " + why
	}
	switch run.State {
	case Reverted, Reverting:
		return run, nil
	case Pending, Expanding:
		e.mu.Lock()
		abort := e.aborts[id]
		e.mu.Unlock()
		if abort != nil {
			abort(errors.New(reason))
		}
	case Expanded:
		sessions, err := e.sessions(ctx)
		if err != nil {
			return Run{}, err
		}
		stranded, err := e.stranded(ctx, sessions, run.Name)
		if err != nil {
			return Run{}, err
		}
		if len(stranded) > 0 {
			return Run{}, conflict("connected application versions can only run on schema version "+run.Name+"; stop them before reverting", stranded...)
		}
		if ok, err := e.move(ctx, id, Expanded, Reverting, ``); err != nil || !ok {
			return Run{}, conflict("run changed state; retry")
		}
		log := e.runLog(run)
		e.spawn(func() {
			// Stop handing out the new version, let its sessions move back, then drop it.
			if _, err := e.db.ExecContext(e.ctx, `UPDATE shipd.schema_versions SET live = false WHERE version = $1`, run.Name); err != nil {
				log.Error("retire version", "err", err)
			}
			if left, _ := e.drain(e.ctx, run.Settings, func(x Session) bool { return x.SchemaVersion == run.Name }); len(left) > 0 {
				log.Warn("sessions still on the reverted version; they will fail and reconnect", "sessions", left)
			}
			e.revert(run, reason, log)
		})
	default:
		return Run{}, conflict(fmt.Sprintf("a %s migration cannot be aborted", run.State))
	}
	return e.Get(ctx, id)
}

// Wait blocks until the run reaches one of the given states or any terminal state.
func (e *Engine) Wait(ctx context.Context, id int64, states ...string) (Run, error) {
	for {
		run, err := e.Get(ctx, id)
		if err != nil {
			return run, err
		}
		for _, s := range append(states, Completed, Reverted, Failed) {
			if run.State == s {
				return run, nil
			}
		}
		if err := sleep(ctx, 100*time.Millisecond); err != nil {
			return run, err
		}
	}
}

func sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
