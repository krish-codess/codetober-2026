package engine

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/lib/pq"
	"github.com/xataio/pgroll/pkg/backfill"
	"github.com/xataio/pgroll/pkg/migrations"
)

// ColumnCheck compares the old and new representation of one column across the whole table.
type ColumnCheck struct {
	Table        string   `json:"table"`
	Column       string   `json:"column"`
	Rows         int64    `json:"rows"`
	Mismatches   int64    `json:"mismatches" doc:"Rows where new != up(old) and old != down(new)"`
	Unbackfilled int64    `json:"unbackfilled" doc:"Rows the backfill has not reached"`
	Lossy        int64    `json:"lossy" doc:"Rows whose old value has no new representation; copied to shipd.quarantine"`
	Examples     []string `json:"examples" doc:"Primary keys of up to five mismatching rows"`
}

type Verification struct {
	OK         bool          `json:"ok"`
	At         time.Time     `json:"at"`
	DurationMs int64         `json:"duration_ms"`
	Checks     []ColumnCheck `json:"checks" doc:"Empty when the migration has no column with two representations"`
}

func (v Verification) problems() []string {
	var out []string
	for _, c := range v.Checks {
		if c.Mismatches > 0 || c.Unbackfilled > 0 {
			out = append(out, fmt.Sprintf("%s.%s: %d mismatching rows, %d not backfilled (e.g. primary keys %s)",
				c.Table, c.Column, c.Mismatches, c.Unbackfilled, strings.Join(c.Examples, " ")))
		}
	}
	return out
}

// Verify proves the old and new representations agree. It only makes sense while both exist.
func (e *Engine) Verify(ctx context.Context, id int64) (Verification, error) {
	run, err := e.Get(ctx, id)
	if err != nil {
		return Verification{}, err
	}
	if run.State != Expanded {
		return Verification{}, conflict(fmt.Sprintf("old and new representations only coexist while a migration is expanded; this one is %s", run.State))
	}
	return e.verify(ctx, run)
}

func (e *Engine) verify(ctx context.Context, run Run) (Verification, error) {
	start := time.Now()
	v := Verification{OK: true, At: start, Checks: []ColumnCheck{}}
	mig, err := parse(run)
	if err != nil {
		return v, err
	}
	for _, op := range mig.Operations {
		ac, ok := op.(*migrations.OpAlterColumn)
		if !ok || ac.Up == "" || ac.Down == "" {
			continue
		}
		newName := ac.Column // a rename_column in the same migration gives the column its name in the new version
		for _, other := range mig.Operations {
			if r, ok := other.(*migrations.OpRenameColumn); ok && r.Table == ac.Table && r.From == ac.Column {
				newName = r.To
			}
		}
		c, err := e.check(ctx, run.ID, ac.Table, ac.Column, newName, ac.Up, ac.Down)
		if err != nil {
			return v, fmt.Errorf("verify %s.%s: %w", ac.Table, ac.Column, err)
		}
		v.OK = v.OK && c.Mismatches == 0 && c.Unbackfilled == 0
		v.Checks = append(v.Checks, c)
	}
	v.DurationMs = time.Since(start).Milliseconds()
	raw, _ := json.Marshal(v)
	e.set(run.ID, `verification = $2`, raw)
	return v, nil
}

// check runs one full scan. `up` and `down` are the migration's own SQL, already trusted to run as DDL.
// A row agrees if either direction reproduces it: rows written by old clients satisfy new = up(old),
// rows written by new clients satisfy old = down(new).
func (e *Engine) check(ctx context.Context, runID int64, table, column, newName, up, down string) (ColumnCheck, error) {
	c := ColumnCheck{Table: table, Column: column, Examples: []string{}}
	qi := pq.QuoteIdentifier
	t, old, nw, nb := appSchema+"."+qi(table), qi(column), qi(migrations.TemporaryName(column)), qi(backfill.CNeedsBackfillColumn)

	var pkCols []string
	if err := e.db.QueryRowContext(ctx, `
		SELECT array_agg(a.attname::text ORDER BY k.ord) FROM pg_index i
		CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord)
		JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
		WHERE i.indrelid = to_regclass($1) AND i.indisprimary`, t).Scan(pq.Array(&pkCols)); err != nil {
		return c, err
	}
	if len(pkCols) == 0 {
		return c, fmt.Errorf("table has no primary key")
	}
	for i, p := range pkCols {
		pkCols[i] = "o." + qi(p) + "::text"
	}
	pk := "concat_ws(',', " + strings.Join(pkCols, ", ") + ")"
	// `down` is written against the new schema version, where the column may have a new name. Inside the scalar
	// subquery that name resolves to the new value, exactly as it does in the trigger pgroll builds.
	agree := fmt.Sprintf(`(o.%[2]s IS NOT DISTINCT FROM (%[3]s) OR o.%[1]s IS NOT DISTINCT FROM (SELECT (%[4]s) FROM (SELECT o.%[2]s AS %[5]s) AS _new))`, old, nw, up, down, qi(newName))
	lossy := fmt.Sprintf(`(o.%s IS NOT NULL AND o.%s IS NULL)`, old, nw)

	if _, err := e.db.ExecContext(ctx, fmt.Sprintf(`
		INSERT INTO shipd.quarantine (run_id, table_name, pk, column_name, raw)
		SELECT $1, $2, %s, $3, to_jsonb(o) FROM %s o WHERE %s ON CONFLICT DO NOTHING`, pk, t, lossy), runID, table, column); err != nil {
		return c, err
	}
	if err := e.db.QueryRowContext(ctx, fmt.Sprintf(`
		SELECT count(*), count(*) FILTER (WHERE NOT %s), count(*) FILTER (WHERE o.%s), count(*) FILTER (WHERE %s) FROM %s o`,
		agree, nb, lossy, t)).Scan(&c.Rows, &c.Mismatches, &c.Unbackfilled, &c.Lossy); err != nil {
		return c, err
	}
	if c.Mismatches > 0 {
		if err := e.db.QueryRowContext(ctx, fmt.Sprintf(`SELECT array_agg(k) FROM (SELECT %s AS k FROM %s o WHERE NOT %s LIMIT 5) x`, pk, t, agree)).Scan(pq.Array(&c.Examples)); err != nil {
			return c, err
		}
	}
	return c, nil
}

// ---- analytics ----

type DurationPoint struct {
	RunID      int64   `json:"run_id"`
	Name       string  `json:"name"`
	State      string  `json:"state"`
	Rows       int64   `json:"rows" doc:"Rows backfilled"`
	Bytes      int64   `json:"bytes" doc:"Size of the tables touched when the migration started"`
	DDLSeconds float64 `json:"ddl_seconds" doc:"Expand DDL, including lock waits and concurrent index builds"`
	BackfillS  float64 `json:"backfill_seconds"`
	ContractS  float64 `json:"contract_seconds" doc:"0 until the migration is completed"`
	RowsPerS   float64 `json:"rows_per_second" doc:"Backfill throughput including throttle pauses"`
	DualWriteS float64 `json:"dual_write_seconds" doc:"How long old and new representations coexisted"`
}

type DurationBySize struct {
	Points           []DurationPoint `json:"points"`
	MedianRowsPerSec float64         `json:"median_rows_per_second" doc:"Median backfill throughput of past runs; 0 with no history"`
}

// DurationBySize reports how long each migration took against how much data it touched.
func (e *Engine) DurationBySize(ctx context.Context) (DurationBySize, error) {
	out := DurationBySize{Points: []DurationPoint{}}
	rows, err := e.db.QueryContext(ctx, `
		SELECT id, name, state, rows_done,
		       COALESCE((SELECT sum((t->>'bytes')::bigint) FROM jsonb_array_elements(tables) t), 0),
		       EXTRACT(epoch FROM ddl_done_at - started_at),
		       EXTRACT(epoch FROM expanded_at - ddl_done_at),
		       COALESCE(EXTRACT(epoch FROM finished_at - contract_started_at), 0),
		       COALESCE(EXTRACT(epoch FROM COALESCE(contract_started_at, now()) - expanded_at), 0)
		FROM shipd.runs WHERE expanded_at IS NOT NULL ORDER BY id`)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var p DurationPoint
		if err := rows.Scan(&p.RunID, &p.Name, &p.State, &p.Rows, &p.Bytes, &p.DDLSeconds, &p.BackfillS, &p.ContractS, &p.DualWriteS); err != nil {
			return out, err
		}
		if p.Rows > 0 && p.BackfillS > 0 {
			p.RowsPerS = float64(p.Rows) / p.BackfillS
		}
		out.Points = append(out.Points, p)
	}
	if err := rows.Err(); err != nil {
		return out, err
	}
	err = e.db.QueryRowContext(ctx, `
		SELECT COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY rows_done / EXTRACT(epoch FROM expanded_at - ddl_done_at)), 0)
		FROM shipd.runs WHERE expanded_at IS NOT NULL AND rows_done > 0 AND expanded_at > ddl_done_at`).Scan(&out.MedianRowsPerSec)
	return out, err
}

type LockWait struct {
	RunID          int64   `json:"run_id"`
	Name           string  `json:"name"`
	State          string  `json:"state"`
	Samples        int     `json:"samples"`
	MaxBlocked     int     `json:"max_blocked" doc:"Most application sessions blocked by the migration at once"`
	MaxWaitMs      int     `json:"max_wait_ms" doc:"Longest an application session was blocked by the migration"`
	P95WaitMs      float64 `json:"p95_wait_ms"`
	BlockedSeconds float64 `json:"blocked_seconds" doc:"Time during which at least one application session was blocked"`
	MigratorWaitS  float64 `json:"migrator_wait_seconds" doc:"Time the migration itself spent waiting for locks"`
}

// LockWaits summarises the guard samples of each run.
func (e *Engine) LockWaits(ctx context.Context) ([]LockWait, error) {
	rows, err := e.db.QueryContext(ctx, `
		SELECT r.id, r.name, r.state, count(s.at), COALESCE(max(s.blocked), 0), COALESCE(max(s.max_wait_ms), 0),
		       COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY s.max_wait_ms), 0),
		       0.5 * count(*) FILTER (WHERE s.blocked > 0), 0.5 * count(*) FILTER (WHERE s.migrator_wait_ms > 0)
		FROM shipd.runs r LEFT JOIN shipd.samples s ON s.run_id = r.id
		WHERE r.started_at IS NOT NULL GROUP BY r.id ORDER BY r.id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []LockWait{}
	for rows.Next() {
		var w LockWait
		if err := rows.Scan(&w.RunID, &w.Name, &w.State, &w.Samples, &w.MaxBlocked, &w.MaxWaitMs, &w.P95WaitMs, &w.BlockedSeconds, &w.MigratorWaitS); err != nil {
			return nil, err
		}
		out = append(out, w)
	}
	return out, rows.Err()
}
