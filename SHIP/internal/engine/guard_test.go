package engine

import (
	"slices"
	"strings"
	"testing"
	"time"
)

func TestBreach(t *testing.T) {
	s := Settings{}.WithDefaults()
	tests := []struct {
		name     string
		x        Sample
		lockWait time.Duration
		want     string
	}{
		{"quiet", Sample{}, 0, ""},
		{"at the limits", Sample{MaxWaitMs: s.MaxLockWaitMs, Blocked: s.MaxBlocked, RollbacksPerS: s.MaxRollbacksPerS}, time.Duration(s.LockBudgetS) * time.Second, ""},
		{"one session blocked too long", Sample{Blocked: 1, MaxWaitMs: s.MaxLockWaitMs + 1}, 0, "max_lock_wait_ms"},
		{"too many sessions blocked", Sample{Blocked: s.MaxBlocked + 1, MaxWaitMs: 5}, 0, "max_blocked"},
		{"migration waited too long for locks", Sample{}, time.Duration(s.LockBudgetS)*time.Second + time.Millisecond, "lock_budget_s"},
		{"errors spike", Sample{RollbacksPerS: s.MaxRollbacksPerS + 0.1}, 0, "max_rollbacks_per_s"},
		{"lock wait outranks errors", Sample{MaxWaitMs: 99999, RollbacksPerS: 99999}, 0, "max_lock_wait_ms"},
	}
	for _, tt := range tests {
		got, reason := breach(s, tt.x, tt.lockWait)
		if got != tt.want || (got == "") != (reason == "") {
			t.Errorf("%s: got %q (%q), want %q", tt.name, got, reason, tt.want)
		}
	}
}

func TestSettingsDefaults(t *testing.T) {
	s := Settings{BatchSize: 7, MaxRollbacksPerS: 3}.WithDefaults()
	if s.BatchSize != 7 || s.MaxRollbacksPerS != 3 {
		t.Errorf("explicit values were overwritten: %+v", s)
	}
	if s.LockTimeoutMs == 0 || s.LockBudgetS == 0 || s.MaxLockWaitMs == 0 || s.MaxBlocked == 0 || s.DrainTimeoutS == 0 || s.ThrottleRatio == 0 {
		t.Errorf("a threshold was left at zero, which would disable it: %+v", s)
	}
	if s.LockTimeoutMs >= s.MaxLockWaitMs {
		t.Errorf("lock_timeout (%d ms) must be below max_lock_wait (%d ms), or ordinary retries would trip the guard", s.LockTimeoutMs, s.MaxLockWaitMs)
	}
}

func TestIncompatible(t *testing.T) {
	sessions := []Session{
		{App: "shop", AppVersion: "v1", SchemaVersion: "01_initial", Count: 8},
		{App: "shop", AppVersion: "v2", SchemaVersion: "02_amount_to_cents", Count: 3},
	}
	if got := incompatible(sessions, []string{"v1", "v2"}); len(got) != 0 {
		t.Errorf("everyone is compatible, got %v", got)
	}
	got := incompatible(sessions, []string{"v2"})
	if len(got) != 1 || !strings.Contains(got[0], "shop v1 on 01_initial (8 sessions)") {
		t.Errorf("got %v", got)
	}
}

func TestOpTables(t *testing.T) {
	ops := []map[string]any{
		{"create_table": map[string]any{"name": "fresh"}},
		{"alter_column": map[string]any{"table": "orders", "column": "amount"}},
		{"rename_column": map[string]any{"table": "orders"}},
		{"create_index": map[string]any{"table": "customers"}},
		{"sql": map[string]any{"up": "SELECT 1"}},
	}
	if got := opTables(ops); !slices.Equal(got, []string{"orders", "customers"}) {
		t.Errorf("got %v", got)
	}
}
