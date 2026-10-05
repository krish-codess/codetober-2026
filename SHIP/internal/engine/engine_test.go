package engine_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/krish-codess/codetober-2026/SHIP/internal/engine"
	"github.com/krish-codess/codetober-2026/SHIP/internal/plan"
	"github.com/krish-codess/codetober-2026/SHIP/internal/shop"
	"github.com/krish-codess/codetober-2026/SHIP/internal/testdb"
)

type writers struct {
	cancel context.CancelFunc
	done   chan shop.Report
}

// startWriters runs live traffic as one application version until stop is called.
func startWriters(t *testing.T, d *testdb.DB, version string) *writers {
	t.Helper()
	db := d.Shop(t, version, 5)
	if err := db.Ping(); err != nil {
		t.Fatalf("%s cannot connect: %v", version, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	w := &writers{cancel, make(chan shop.Report, 1)}
	go func() {
		rep := shop.Run(ctx, db, shop.Config{Version: version, Workers: 4, Rate: 150, Seed: 7, Name: version + "t"})
		db.Close() // sessions must be gone before the report is, so gates see them leave
		w.done <- rep
	}()
	t.Cleanup(cancel)
	return w
}

func (w *writers) stop(t *testing.T) shop.Report {
	t.Helper()
	w.cancel()
	rep := <-w.done
	t.Logf("%s writers: %d inserts, %d updates, %d reads, %d rows checked, p50 %.1f ms, p99 %.1f ms, max %.1f ms, %d retries",
		rep.Version, rep.Inserts, rep.Updates, rep.Reads, rep.Checked, rep.P50Ms, rep.P99Ms, rep.MaxMs, rep.Retries)
	if rep.Errors != 0 || rep.Mismatches != 0 {
		t.Errorf("%s writers saw %d errors and %d mismatches: %v", rep.Version, rep.Errors, rep.Mismatches, rep.Samples)
	}
	if rep.Inserts == 0 || rep.Checked == 0 {
		t.Errorf("%s writers did no work; the test proves nothing", rep.Version)
	}
	if rep.MaxMs > 5000 {
		t.Errorf("%s writers were stalled for %.0f ms", rep.Version, rep.MaxMs)
	}
	return rep
}

func scalar[T any](t *testing.T, d *testdb.DB, query string, args ...any) T {
	t.Helper()
	db, err := sql.Open("postgres", d.Admin)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var v T
	if err := db.QueryRow(query, args...).Scan(&v); err != nil {
		t.Fatalf("%s: %v", query, err)
	}
	return v
}

func waitFor(t *testing.T, eng *engine.Engine, id int64, state string) engine.Run {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	run, err := eng.Wait(ctx, id, state)
	if err != nil || run.State != state {
		t.Fatalf("run %d: want %s, got %s (reason %q, err %v)", id, state, run.State, run.Reason, err)
	}
	return run
}

// planned checks that the diff planner, given desired/<name>.json and the live schema, produces the committed migration.
func planned(t *testing.T, eng *engine.Engine, name string) {
	t.Helper()
	var desired plan.Desired
	raw, err := os.ReadFile(filepath.Join(testdb.Root(), "desired", name+".json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &desired); err != nil {
		t.Fatal(err)
	}
	current, err := plan.Load(context.Background(), eng.DB())
	if err != nil {
		t.Fatal(err)
	}
	p, err := plan.Diff(current, desired, 0)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := json.Marshal(p.Operations)
	want, _ := json.Marshal(testdb.Migration(t, name).Operations)
	if string(got) != string(want) {
		t.Errorf("migrations/%s.json is not what the planner produces from desired/%s.json\n got: %s\nwant: %s", name, name, got, want)
	}
}

// The headline claim: a column changes type and name on a table with active writers of two application
// versions, nobody sees an error or a wrong value, and the old column is dropped only after both
// representations are proven equal and the old version is gone.
func TestZeroDowntimeMigrationUnderLoad(t *testing.T) {
	d := testdb.New(t)
	eng := d.Engine(t)
	ctx := context.Background()
	d.Seed(t, eng, 20000)
	planned(t, eng, "02_amount_to_cents")

	v1 := startWriters(t, d, "v1")
	if c, err := eng.Check(ctx, "v2"); err != nil || c.Allowed {
		t.Fatalf("v2 must not be deployable before its schema version exists: %+v %v", c, err)
	}

	in := testdb.Migration(t, "02_amount_to_cents")
	in.Settings.BatchSize = 500
	run := testdb.Apply(t, eng, in, false)
	if c, err := eng.Check(ctx, "v2"); err != nil || !c.Allowed || c.SchemaVersion != "02_amount_to_cents" {
		t.Fatalf("v2 should be deployable once expanded: %+v %v", c, err)
	}

	v2 := startWriters(t, d, "v2") // old and new application versions, both live, reading each other's writes
	time.Sleep(3 * time.Second)

	// The gate: v1 is still connected and cannot run on the new schema, so the old column must not be dropped.
	for {
		_, err := eng.Complete(ctx, run.ID)
		if err == nil {
			t.Fatal("complete succeeded while v1 was still connected")
		}
		if strings.Contains(err.Error(), "not finished") {
			time.Sleep(50 * time.Millisecond)
			continue
		}
		if e, ok := err.(*engine.Error); !ok || e.Kind != "conflict" || !strings.Contains(strings.Join(e.Details, " "), "shop v1") {
			t.Fatalf("want a conflict naming shop v1, got %v", testdb.Describe(err))
		}
		break
	}

	r1 := v1.stop(t)
	done := testdb.Complete(t, eng, run.ID)

	if v := done.Verification; v == nil || !v.OK || len(v.Checks) != 1 || v.Checks[0].Mismatches != 0 || v.Checks[0].Unbackfilled != 0 {
		t.Fatalf("verification: %+v", v)
	}
	lossy := done.Verification.Checks[0].Lossy
	if q := scalar[int64](t, d, `SELECT count(*) FROM shipd.quarantine WHERE run_id = $1`, run.ID); q != lossy || lossy < r1.Unreadable || lossy == 0 {
		t.Errorf("quarantined %d rows, verification reported %d lossy, v1 wrote %d unreadable amounts", q, lossy, r1.Unreadable)
	}
	if cols := scalar[string](t, d, `SELECT string_agg(column_name || ':' || data_type, ',' ORDER BY column_name) FROM information_schema.columns
		WHERE table_schema = 'public' AND table_name = 'orders' AND column_name LIKE '%amount%'`); cols != "amount_cents:bigint" {
		t.Errorf("after contract the amount columns are %q", cols)
	}

	// A second migration, planned from a diff: started, aborted (v2 falls back to the previous version), then applied.
	planned(t, eng, "03_orders_pending_idx")
	before := d.Snapshot(t)
	idx := testdb.Apply(t, eng, testdb.Migration(t, "03_orders_pending_idx"), false)
	time.Sleep(6 * time.Second) // v2's connections recycle onto the newer version
	if _, err := eng.Abort(ctx, idx.ID, "test"); err != nil {
		t.Fatalf("abort: %v", testdb.Describe(err))
	}
	waitFor(t, eng, idx.ID, engine.Reverted)
	if after := d.Snapshot(t); after != before {
		t.Errorf("revert of 03 left the schema changed:\n%s\n---\n%s", before, after)
	}
	testdb.Apply(t, eng, testdb.Migration(t, "03_orders_pending_idx"), true)
	time.Sleep(time.Second)

	r2 := v2.stop(t)
	if n := scalar[int64](t, d, `SELECT count(*) FROM public.orders WHERE order_ref LIKE 'w-%'`); n != r1.Inserts+r2.Inserts {
		t.Errorf("%d rows written, %d acknowledged: writes were lost or duplicated", n, r1.Inserts+r2.Inserts)
	}

	// The analytics have something to say about what just happened.
	dur, err := eng.DurationBySize(ctx)
	if err != nil || len(dur.Points) < 3 || dur.MedianRowsPerSec <= 0 {
		t.Errorf("duration by size: %+v %v", dur, err)
	}
	waits, err := eng.LockWaits(ctx)
	if err != nil || len(waits) < 4 {
		t.Errorf("lock waits: %+v %v", waits, err)
	}
	samples, err := eng.Samples(ctx, run.ID, time.Time{}, 10)
	if err != nil || len(samples) == 0 {
		t.Errorf("no guard samples for the run: %v", err)
	}
}

// Every committed migration has a rollback path, and it leaves nothing behind while v1 keeps writing.
func TestAbortRestoresSchemaExactly(t *testing.T) {
	d := testdb.New(t)
	eng := d.Engine(t)
	d.Seed(t, eng, 5000)
	before := d.Snapshot(t)
	v1 := startWriters(t, d, "v1")

	run := testdb.Apply(t, eng, testdb.Migration(t, "02_amount_to_cents"), false)
	if d.Snapshot(t) == before {
		t.Fatal("expand changed nothing; the test proves nothing")
	}
	if _, err := eng.Abort(context.Background(), run.ID, "changed our minds"); err != nil {
		t.Fatal(testdb.Describe(err))
	}
	run = waitFor(t, eng, run.ID, engine.Reverted)
	if !strings.Contains(run.Reason, "changed our minds") {
		t.Errorf("reason %q", run.Reason)
	}
	if again, err := eng.Abort(context.Background(), run.ID, ""); err != nil || again.State != engine.Reverted {
		t.Errorf("abort is not idempotent: %v", err)
	}
	time.Sleep(500 * time.Millisecond)
	v1.stop(t)
	if after := d.Snapshot(t); after != before {
		t.Errorf("revert left the schema changed:\n%s\n---\n%s", before, after)
	}
}

// A long-running transaction holds the table. The migration must give up within its lock budget,
// change nothing, and not stall the writers queued behind its lock requests.
func TestRevertsWhenLockBudgetExceeded(t *testing.T) {
	d := testdb.New(t)
	eng := d.Engine(t)
	d.Seed(t, eng, 2000)
	before := d.Snapshot(t)
	v1 := startWriters(t, d, "v1")

	admin, err := sql.Open("postgres", d.Admin)
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	tx, err := admin.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`LOCK TABLE public.orders IN ACCESS SHARE MODE`); err != nil { // what an open report query holds
		t.Fatal(err)
	}

	in := testdb.Migration(t, "02_amount_to_cents")
	in.Settings = engine.Settings{LockTimeoutMs: 100, LockBudgetS: 2}
	start := time.Now()
	run, _, err := eng.Submit(context.Background(), in, "test")
	if err != nil {
		t.Fatal(testdb.Describe(err))
	}
	run = waitFor(t, eng, run.ID, engine.Reverted)
	if !strings.Contains(run.Reason, "could not lock orders") {
		t.Errorf("reason %q", run.Reason)
	}
	if took := time.Since(start); took > 15*time.Second {
		t.Errorf("took %s to give up on a 2 s lock budget", took)
	}
	if after := d.Snapshot(t); after != before {
		t.Errorf("schema changed:\n%s\n---\n%s", before, after)
	}
	tx.Rollback()

	// Once the transaction is gone the same migration goes through: a reverted run does not block a retry.
	testdb.Apply(t, eng, testdb.Migration(t, "02_amount_to_cents"), false)
	v1.stop(t)
}

// A migration whose up expression cannot handle the data that is really there fails in the backfill and is rolled back.
func TestRevertsWhenMigrationErrors(t *testing.T) {
	d := testdb.New(t)
	eng := d.Engine(t)
	d.Seed(t, eng, 2000)
	before := d.Snapshot(t)

	in := testdb.Migration(t, "02_amount_to_cents")
	in.Operations[0]["alter_column"].(map[string]any)["up"] = "(amount::numeric * 100)::bigint" // fine on a clean sample
	run, _, err := eng.Submit(context.Background(), in, "test")
	if err != nil {
		t.Fatal(testdb.Describe(err))
	}
	run = waitFor(t, eng, run.ID, engine.Reverted)
	if !strings.Contains(run.Reason, "invalid input syntax") {
		t.Errorf("reason %q", run.Reason)
	}
	if after := d.Snapshot(t); after != before {
		t.Errorf("schema changed:\n%s\n---\n%s", before, after)
	}
}

// Application errors spike while a migration is expanding: the guard aborts it and reverts.
func TestGuardRevertsOnErrorRate(t *testing.T) {
	d := testdb.New(t)
	eng := d.Engine(t)
	d.Seed(t, eng, 20000)
	before := d.Snapshot(t)

	in := testdb.Migration(t, "02_amount_to_cents")
	in.Settings = engine.Settings{BatchSize: 100, BatchDelayMs: 100, MaxRollbacksPerS: 5} // slow enough to be caught mid-backfill
	run, _, err := eng.Submit(context.Background(), in, "test")
	if err != nil {
		t.Fatal(testdb.Describe(err))
	}
	waitFor(t, eng, run.ID, engine.Expanding)

	app := d.Shop(t, "v1", 2)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { // a client failing a few hundred times a second
		for ctx.Err() == nil {
			_, _ = app.ExecContext(ctx, `SELECT 1/0`)
			time.Sleep(2 * time.Millisecond)
		}
	}()

	run = waitFor(t, eng, run.ID, engine.Reverted)
	if !strings.Contains(run.Reason, "rolled-back transactions per second") {
		t.Errorf("reason %q", run.Reason)
	}
	if after := d.Snapshot(t); after != before {
		t.Errorf("schema changed:\n%s\n---\n%s", before, after)
	}
}

// Failure injection: the controller dies in the middle of a backfill. Writers must not notice, the next
// controller must roll the half-done migration back, and a retry must then succeed with no write lost or doubled.
func TestRecoversFromCrashMidBackfill(t *testing.T) {
	d := testdb.New(t)
	eng := d.Engine(t)
	ctx := context.Background()
	d.Seed(t, eng, 20000)
	before := d.Snapshot(t)
	v1 := startWriters(t, d, "v1")

	in := testdb.Migration(t, "02_amount_to_cents")
	in.Settings = engine.Settings{BatchSize: 100, BatchDelayMs: 50}
	run, _, err := eng.Submit(ctx, in, "test")
	if err != nil {
		t.Fatal(testdb.Describe(err))
	}
	for deadline := time.Now().Add(time.Minute); ; time.Sleep(50 * time.Millisecond) {
		if r, _ := eng.Get(ctx, run.ID); r.RowsDone >= 1000 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("backfill never started")
		}
	}
	eng.Close() // the crash: no cleanup runs

	if state := scalar[string](t, d, `SELECT state FROM shipd.runs WHERE id = $1`, run.ID); state != engine.Expanding {
		t.Fatalf("after the crash the run is %s", state)
	}
	if d.Snapshot(t) == before {
		t.Fatal("nothing was mid-flight when the controller died")
	}
	time.Sleep(time.Second) // writers carry on against the half-migrated table

	eng = d.Engine(t) // the replacement controller
	run, err = eng.Get(ctx, run.ID)
	if err != nil || run.State != engine.Reverted || !strings.Contains(run.Reason, "controller restarted") {
		t.Fatalf("after recovery: state %s, reason %q, err %v", run.State, run.Reason, err)
	}
	if after := d.Snapshot(t); after != before {
		t.Errorf("recovery left the schema changed:\n%s\n---\n%s", before, after)
	}

	retry := testdb.Apply(t, eng, testdb.Migration(t, "02_amount_to_cents"), false)
	r1 := v1.stop(t)
	done := testdb.Complete(t, eng, retry.ID)
	if !done.Verification.OK {
		t.Errorf("verification: %+v", done.Verification)
	}
	if n := scalar[int64](t, d, `SELECT count(*) FROM public.orders WHERE order_ref LIKE 'w-%'`); n != r1.Inserts {
		t.Errorf("%d rows written, %d acknowledged: writes were lost or duplicated", n, r1.Inserts)
	}
}

func TestSubmitIsIdempotentAndValidated(t *testing.T) {
	d := testdb.New(t)
	eng := d.Engine(t)
	ctx := context.Background()
	d.Seed(t, eng, 200)

	in := testdb.Migration(t, "02_amount_to_cents")
	first := testdb.Apply(t, eng, in, false)
	second, created, err := eng.Submit(ctx, in, "retry")
	if err != nil || created || second.ID != first.ID {
		t.Errorf("resubmitting returned run %d created=%v err=%v, want run %d", second.ID, created, err, first.ID)
	}

	different := testdb.Migration(t, "02_amount_to_cents")
	different.Operations = different.Operations[:1]
	if _, _, err := eng.Submit(ctx, different, "x"); !isKind(err, "conflict") {
		t.Errorf("a different migration under the same name: %v", err)
	}
	other := testdb.Migration(t, "03_orders_pending_idx")
	if _, _, err := eng.Submit(ctx, other, "x"); !isKind(err, "conflict") && !isKind(err, "invalid") {
		t.Errorf("a second migration while one is in flight: %v", err)
	}
	bogus := engine.Submit{Name: "09_bogus", CompatibleAppVersions: []string{"v2"}, Operations: []map[string]any{{"teleport_column": map[string]any{}}}}
	if _, _, err := eng.Submit(ctx, bogus, "x"); !isKind(err, "invalid") {
		t.Errorf("an unknown operation: %v", err)
	}
	if _, err := eng.Get(ctx, 9999); !isKind(err, "not_found") {
		t.Errorf("a missing run: %v", err)
	}
	if _, err := eng.Verify(ctx, 1); !isKind(err, "conflict") { // run 1 is completed: nothing left to compare
		t.Errorf("verifying a completed run: %v", err)
	}
}

func isKind(err error, kind string) bool {
	e, ok := err.(*engine.Error)
	return ok && e.Kind == kind
}
