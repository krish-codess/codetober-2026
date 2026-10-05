package plan

import (
	"encoding/json"
	"strings"
	"testing"
)

func current() Schema {
	return Schema{"orders": Table{
		Columns: []Column{
			{Name: "id", Type: "bigint"},
			{Name: "amount", Type: "text", Nullable: true},
			{Name: "status", Type: "text", Checks: []string{"orders_status_valid"}},
			{Name: "note", Type: "character varying(40)", Nullable: true},
		},
		Indexes: []string{"idx_old"},
		Rows:    1000, Bytes: 8192,
	}}
}

func cols(extra ...DesiredColumn) []DesiredColumn {
	base := []DesiredColumn{{Name: "id", Type: "int8"}, {Name: "status", Type: "text"}, {Name: "note", Type: "varchar(40)", Nullable: true}}
	return append(base, extra...)
}

func ops(t *testing.T, p Plan) string {
	t.Helper()
	var b strings.Builder
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false) // keep ">=" readable in the expectations
	if err := enc.Encode(p.Operations); err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(b.String())
}

func TestDiff(t *testing.T) {
	amount := DesiredColumn{Name: "amount", Type: "text", Nullable: true}
	tests := []struct {
		name    string
		table   DesiredTable
		want    string // exact operations JSON, or
		wantErr string // a fragment of the error
	}{
		{
			name:    "identical schema, spelled with type aliases",
			table:   DesiredTable{Columns: cols(amount)},
			wantErr: "nothing to migrate",
		},
		{
			name: "type change plus rename plus check: the check is rewritten against the current column name",
			table: DesiredTable{Columns: cols(DesiredColumn{Name: "amount_cents", Type: "bigint", Nullable: true, RenamedFrom: "amount",
				Check: &Check{Name: "nonneg", Constraint: "amount_cents >= 0"}, Up: "parse(amount)", Down: "format(amount)"})},
			want: `[{"alter_column":{"check":{"constraint":"amount >= 0","name":"nonneg"},"column":"amount","down":"format(amount)","table":"orders","type":"bigint","up":"parse(amount)"}},` +
				`{"rename_column":{"from":"amount","table":"orders","to":"amount_cents"}}]`,
		},
		{
			name:    "type change without up and down cannot keep both application versions working",
			table:   DesiredTable{Columns: cols(DesiredColumn{Name: "amount", Type: "bigint", Nullable: true})},
			wantErr: "needs both up and down",
		},
		{
			name:  "set not null",
			table: DesiredTable{Columns: cols(DesiredColumn{Name: "amount", Type: "text", Up: "COALESCE(amount, '0')", Down: "amount"})},
			want:  `[{"alter_column":{"column":"amount","down":"amount","nullable":false,"table":"orders","up":"COALESCE(amount, '0')"}}]`,
		},
		{
			name:  "pure rename needs no backfill",
			table: DesiredTable{Columns: cols(DesiredColumn{Name: "total", Type: "text", Nullable: true, RenamedFrom: "amount"})},
			want:  `[{"rename_column":{"from":"amount","table":"orders","to":"total"}}]`,
		},
		{
			name:  "new nullable column",
			table: DesiredTable{Columns: cols(amount, DesiredColumn{Name: "channel", Type: "text", Nullable: true})},
			want:  `[{"add_column":{"column":{"name":"channel","nullable":true,"type":"text"},"table":"orders"}}]`,
		},
		{
			name:    "new NOT NULL column with nothing to fill it",
			table:   DesiredTable{Columns: cols(amount, DesiredColumn{Name: "channel", Type: "text"})},
			wantErr: "needs a default or an up expression",
		},
		{
			name:    "a column missing from the desired schema is not dropped by accident",
			table:   DesiredTable{Columns: cols()},
			wantErr: "orders.amount exists but is not in the desired schema",
		},
		{
			name:  "explicit drop with a down expression",
			table: DesiredTable{Columns: cols(), Drop: map[string]string{"amount": "'0.00'"}},
			want:  `[{"drop_column":{"column":"amount","down":"'0.00'","table":"orders"}}]`,
		},
		{
			name:    "drop of a column that does not exist",
			table:   DesiredTable{Columns: cols(amount), Drop: map[string]string{"ghost": "1"}},
			wantErr: `drop lists "ghost"`,
		},
		{
			name:    "rename from a column that does not exist",
			table:   DesiredTable{Columns: cols(amount, DesiredColumn{Name: "x", Type: "text", Nullable: true, RenamedFrom: "ghost"})},
			wantErr: `renamed_from "ghost" is not a column`,
		},
		{
			name: "indexes: create the missing one, drop the unlisted one, leave the rest",
			table: DesiredTable{Columns: cols(amount), Indexes: &[]DesiredIndex{
				{Name: "idx_pending", Columns: []string{"id"}, Predicate: "status = 'pending'"}}},
			want: `[{"create_index":{"columns":[{"column":"id"}],"name":"idx_pending","predicate":"status = 'pending'","table":"orders"}},{"drop_index":{"name":"idx_old"}}]`,
		},
		{
			name:    "an existing check is not added twice",
			table:   DesiredTable{Columns: []DesiredColumn{{Name: "id", Type: "bigint"}, amount, {Name: "note", Type: "varchar(40)", Nullable: true}, {Name: "status", Type: "text", Check: &Check{Name: "orders_status_valid", Constraint: "true"}}}},
			wantErr: "nothing to migrate",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p, err := Diff(current(), Desired{Name: "m", CompatibleAppVersions: []string{"v2"}, Tables: map[string]DesiredTable{"orders": tt.table}}, 100)
			if tt.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
					t.Fatalf("want error containing %q, got %v", tt.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got := ops(t, p); got != tt.want {
				t.Errorf("operations\n got: %s\nwant: %s", got, tt.want)
			}
		})
	}
}

func TestDiffReportsEveryProblemAtOnce(t *testing.T) {
	_, err := Diff(current(), Desired{Tables: map[string]DesiredTable{
		"orders": {Columns: []DesiredColumn{{Name: "id", Type: "text"}}},
		"ghosts": {Columns: []DesiredColumn{{Name: "id", Type: "text"}}},
	}}, 0)
	pe, ok := err.(*Error)
	if !ok || len(pe.Problems) != 5 { // missing table, id needs up/down, three columns not listed
		t.Fatalf("want 5 problems, got %v", err)
	}
}

func TestPlanStepsAndEstimate(t *testing.T) {
	p, err := Diff(current(), Desired{Tables: map[string]DesiredTable{"orders": {Columns: cols(DesiredColumn{
		Name: "amount_cents", Type: "bigint", Nullable: true, RenamedFrom: "amount", Up: "1", Down: "'1'"})}}}, 250)
	if err != nil {
		t.Fatal(err)
	}
	last := ""
	seen := map[string]bool{}
	for _, s := range p.Steps {
		if phaseOrder[s.Phase] < phaseOrder[last] {
			t.Errorf("steps out of phase order: %s after %s", s.Phase, last)
		}
		last, seen[s.Phase] = s.Phase, true
		if s.Lock == "" || s.Blocks == "" {
			t.Errorf("step %q does not say what it locks", s.Action)
		}
	}
	if !seen["expand"] || !seen["backfill"] || !seen["contract"] {
		t.Errorf("a type change must have all three phases, got %v", seen)
	}
	if len(p.Estimates) != 1 || p.Estimates[0].BackfillSeconds != 4 { // 1000 rows at 250 rows/s
		t.Errorf("estimate: %+v", p.Estimates)
	}
}

func TestCanonAndRenameIdent(t *testing.T) {
	for in, want := range map[string]string{
		"INT8": "bigint", "timestamptz": "timestamp with time zone", "varchar(40)": "character varying(40)",
		"character  varying (40)": "character varying(40)", "char(3)": "character(3)", "text": "text",
	} {
		if got := canon(in); got != want {
			t.Errorf("canon(%q) = %q, want %q", in, got, want)
		}
	}
	for _, tt := range [][4]string{
		{"amount_cents >= 0", "amount_cents", "amount", "amount >= 0"},
		{"amount_cents_total > amount_cents", "amount_cents", "a", "amount_cents_total > a"},
		{"x > 0", "x", "x", "x > 0"},
		{"lower(email) = email", "email", "e", "lower(e) = e"},
	} {
		if got := renameIdent(tt[0], tt[1], tt[2]); got != tt[3] {
			t.Errorf("renameIdent(%q, %q, %q) = %q, want %q", tt[0], tt[1], tt[2], got, tt[3])
		}
	}
}
