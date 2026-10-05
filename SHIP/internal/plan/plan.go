// Package plan turns the difference between the live schema and a desired schema
// into an expand-contract migration: pgroll operations plus the steps and locks they imply.
package plan

import (
	"fmt"
	"slices"
	"sort"
	"strings"
)

// ---- current schema (from the catalog) ----

type Column struct {
	Name     string
	Type     string // as printed by format_type()
	Nullable bool
	Checks   []string // names of check constraints on this column alone
}

type Table struct {
	Columns []Column
	Indexes []string // names, excluding indexes that back a primary key or unique constraint
	Rows    int64
	Bytes   int64
}

type Schema map[string]Table

// ---- desired schema (written by a person) ----

type Check struct {
	Name       string `json:"name" pattern:"^[a-z0-9_]{1,63}$"`
	Constraint string `json:"constraint" minLength:"1" doc:"SQL boolean expression"`
}

type DesiredColumn struct {
	Name        string  `json:"name" pattern:"^[a-z_][a-z0-9_]{0,62}$"`
	Type        string  `json:"type" minLength:"1" example:"bigint"`
	Nullable    bool    `json:"nullable,omitempty"`
	Default     *string `json:"default,omitempty" doc:"SQL expression; only used when the column is new"`
	Check       *Check  `json:"check,omitempty"`
	RenamedFrom string  `json:"renamed_from,omitempty" doc:"Current name of this column. A diff cannot tell a rename from a drop plus an add, so say so."`
	Up          string  `json:"up,omitempty" doc:"SQL expression computing the new value from the old row"`
	Down        string  `json:"down,omitempty" doc:"SQL expression computing the old value from the new row"`
}

type DesiredIndex struct {
	Name      string   `json:"name" pattern:"^[a-z_][a-z0-9_]{0,62}$"`
	Columns   []string `json:"columns" minItems:"1"`
	Unique    bool     `json:"unique,omitempty"`
	Predicate string   `json:"predicate,omitempty" doc:"WHERE clause of a partial index"`
}

type DesiredTable struct {
	Columns []DesiredColumn   `json:"columns" minItems:"1" doc:"Every column the table should have. A current column that is missing here is dropped."`
	Drop    map[string]string `json:"drop,omitempty" doc:"Columns being dropped, each with the down expression that keeps old clients working"`
	Indexes *[]DesiredIndex   `json:"indexes,omitempty" doc:"Every non-constraint index the table should have. Omit to leave indexes alone."`
}

type Desired struct {
	Name                  string                  `json:"name" pattern:"^[a-z0-9_]{1,40}$" example:"02_amount_to_cents"`
	CompatibleAppVersions []string                `json:"compatible_app_versions" minItems:"1"`
	Tables                map[string]DesiredTable `json:"tables" minProperties:"1" doc:"Tables to change. Tables not listed are left alone."`
}

// ---- the plan ----

type Step struct {
	Phase  string `json:"phase" enum:"expand,backfill,contract"`
	Table  string `json:"table"`
	Action string `json:"action"`
	Lock   string `json:"lock" doc:"Strongest table lock the step takes"`
	Blocks string `json:"blocks" doc:"What that lock blocks, and for how long"`
}

type Estimate struct {
	Table           string  `json:"table"`
	Rows            int64   `json:"rows"`
	Bytes           int64   `json:"bytes"`
	BackfillSeconds float64 `json:"backfill_seconds" doc:"rows / median throughput of past backfills; 0 when there is no history or nothing to backfill"`
}

type Plan struct {
	Name                  string           `json:"name"`
	CompatibleAppVersions []string         `json:"compatible_app_versions"`
	Operations            []map[string]any `json:"operations" doc:"pgroll operations. POST name, compatible_app_versions and operations to /v1/migrations to run them."`
	Steps                 []Step           `json:"steps"`
	Estimates             []Estimate       `json:"estimates"`
}

var aliases = map[string]string{
	"int8": "bigint", "int": "integer", "int4": "integer", "int2": "smallint", "bool": "boolean",
	"timestamptz": "timestamp with time zone", "timestamp": "timestamp without time zone",
	"varchar": "character varying", "char": "character", "float8": "double precision", "decimal": "numeric",
}

func canon(t string) string {
	t = strings.ToLower(strings.Join(strings.Fields(t), " "))
	base, mod := t, ""
	if i := strings.IndexByte(t, '('); i >= 0 {
		base, mod = strings.TrimSpace(t[:i]), t[i:]
	}
	if a, ok := aliases[base]; ok {
		base = a
	}
	return base + mod
}

const (
	brief   = "reads and writes, for the instant the catalog changes; gives up after lock_timeout and retries"
	nobody  = "nothing applications do"
	ddlOnly = "nothing applications do (conflicts only with other DDL)"
)

// Error lists every reason a diff cannot be planned, so they can all be fixed in one pass.
type Error struct{ Problems []string }

func (e *Error) Error() string { return strings.Join(e.Problems, "; ") }

var phaseOrder = map[string]int{"expand": 0, "backfill": 1, "contract": 2}

// Diff computes the migration that takes `current` to `desired`. rowsPerSec is past backfill throughput (0 = unknown).
func Diff(current Schema, desired Desired, rowsPerSec float64) (Plan, error) {
	p := Plan{Name: desired.Name, CompatibleAppVersions: desired.CompatibleAppVersions, Operations: []map[string]any{}, Steps: []Step{}, Estimates: []Estimate{}}
	var problems []string
	bad := func(format string, a ...any) { problems = append(problems, fmt.Sprintf(format, a...)) }

	names := make([]string, 0, len(desired.Tables))
	for n := range desired.Tables {
		names = append(names, n)
	}
	sort.Strings(names)

	for _, tn := range names {
		want := desired.Tables[tn]
		have, ok := current[tn]
		if !ok {
			bad("table %q does not exist; creating tables is not planned from a diff, write a create_table migration", tn)
			continue
		}
		cur := map[string]Column{}
		for _, c := range have.Columns {
			cur[c.Name] = c
		}
		kept := map[string]bool{}
		backfills := false
		step := func(phase, action, lock, blocks string) {
			p.Steps = append(p.Steps, Step{Phase: phase, Table: tn, Action: action, Lock: lock, Blocks: blocks})
		}

		for _, w := range want.Columns {
			src := w.Name
			if w.RenamedFrom != "" {
				src = w.RenamedFrom
			}
			c, exists := cur[src]
			if !exists && w.RenamedFrom != "" {
				bad("%s.%s: renamed_from %q is not a column of the table", tn, w.Name, src)
				continue
			}
			kept[src] = true

			if !exists { // new column
				if !w.Nullable && w.Default == nil && w.Up == "" {
					bad("%s.%s: a new NOT NULL column needs a default or an up expression to fill existing rows", tn, w.Name)
					continue
				}
				col := map[string]any{"name": w.Name, "type": w.Type, "nullable": w.Nullable}
				if w.Default != nil {
					col["default"] = *w.Default
				}
				if w.Check != nil {
					col["check"] = map[string]any{"name": w.Check.Name, "constraint": w.Check.Constraint}
				}
				op := map[string]any{"table": tn, "column": col}
				step("expand", "add column "+w.Name+" (hidden from the old schema version)", "ACCESS EXCLUSIVE", brief)
				if w.Up != "" {
					op["up"] = w.Up
					backfills = true
					step("expand", "trigger: writes through the old version fill "+w.Name, "SHARE ROW EXCLUSIVE", ddlOnly)
					step("contract", "drop the backfill trigger for "+w.Name, "ACCESS EXCLUSIVE", brief)
				}
				p.Operations = append(p.Operations, map[string]any{"add_column": op})
				continue
			}

			// existing column: what changed?
			op := map[string]any{"table": tn, "column": src}
			var changes []string
			if canon(c.Type) != canon(w.Type) {
				op["type"] = w.Type
				changes = append(changes, "type "+c.Type+" -> "+w.Type)
			}
			if c.Nullable != w.Nullable {
				op["nullable"] = w.Nullable
				changes = append(changes, map[bool]string{true: "drop NOT NULL", false: "set NOT NULL"}[w.Nullable])
			}
			constrained := !w.Nullable && c.Nullable
			if w.Check != nil && !slices.Contains(c.Checks, w.Check.Name) {
				// The expression is written against the final column name; pgroll evaluates it against the current one.
				op["check"] = map[string]any{"name": w.Check.Name, "constraint": renameIdent(w.Check.Constraint, w.Name, src)}
				changes = append(changes, "add check "+w.Check.Name)
				constrained = true
			}
			if len(changes) > 0 {
				if w.Up == "" || w.Down == "" {
					bad("%s.%s: %s needs both up and down expressions, so old and new application versions can each read what the other wrote", tn, w.Name, strings.Join(changes, ", "))
					continue
				}
				op["up"], op["down"] = w.Up, w.Down
				p.Operations = append(p.Operations, map[string]any{"alter_column": op})
				backfills = true
				step("expand", "add shadow column for "+src+": "+strings.Join(changes, ", "), "ACCESS EXCLUSIVE", brief)
				if constrained {
					step("expand", "add NOT VALID check constraints on the shadow column of "+src, "ACCESS EXCLUSIVE", brief)
					step("contract", "validate constraints on "+src+" (full scan)", "SHARE UPDATE EXCLUSIVE", nobody)
				}
				step("expand", "triggers: writes through either schema version fill the other representation of "+src, "SHARE ROW EXCLUSIVE", ddlOnly)
				step("contract", "drop old "+src+", promote the shadow column, drop triggers", "ACCESS EXCLUSIVE", brief)
			}
			if src != w.Name {
				p.Operations = append(p.Operations, map[string]any{"rename_column": map[string]any{"table": tn, "from": src, "to": w.Name}})
				step("expand", "expose "+src+" as "+w.Name+" in the new schema version (a view; the table is untouched)", "none", nobody)
				step("contract", "rename "+src+" to "+w.Name, "ACCESS EXCLUSIVE", brief)
			}
		}

		for _, c := range have.Columns {
			if kept[c.Name] {
				continue
			}
			down := want.Drop[c.Name]
			if down == "" {
				bad("%s.%s exists but is not in the desired schema; to drop it, list it under drop with a down expression", tn, c.Name)
				continue
			}
			p.Operations = append(p.Operations, map[string]any{"drop_column": map[string]any{"table": tn, "column": c.Name, "down": down}})
			backfills = true
			step("expand", "hide "+c.Name+" from the new schema version; trigger keeps it filled for the old one", "SHARE ROW EXCLUSIVE", ddlOnly)
			step("contract", "drop column "+c.Name, "ACCESS EXCLUSIVE", brief)
		}
		for name := range want.Drop {
			if _, ok := cur[name]; !ok {
				bad("%s: drop lists %q, which is not a column of the table", tn, name)
			}
		}

		if want.Indexes != nil {
			var wanted []string
			for _, ix := range *want.Indexes {
				wanted = append(wanted, ix.Name)
				if slices.Contains(have.Indexes, ix.Name) {
					continue
				}
				cols := []map[string]any{}
				for _, c := range ix.Columns {
					cols = append(cols, map[string]any{"column": c})
				}
				op := map[string]any{"name": ix.Name, "table": tn, "columns": cols}
				if ix.Unique {
					op["unique"] = true
				}
				if ix.Predicate != "" {
					op["predicate"] = ix.Predicate
				}
				p.Operations = append(p.Operations, map[string]any{"create_index": op})
				step("expand", "create index concurrently "+ix.Name, "SHARE UPDATE EXCLUSIVE", "nothing applications do; waits for transactions older than itself to finish")
			}
			for _, name := range have.Indexes {
				if !slices.Contains(wanted, name) {
					p.Operations = append(p.Operations, map[string]any{"drop_index": map[string]any{"name": name}})
					step("contract", "drop index concurrently "+name, "SHARE UPDATE EXCLUSIVE", nobody)
				}
			}
		}

		est := Estimate{Table: tn, Rows: have.Rows, Bytes: have.Bytes}
		if backfills {
			step("backfill", fmt.Sprintf("touch all ~%d rows in primary-key order, in throttled batches", have.Rows), "ROW EXCLUSIVE", "only writers of a row in the current batch, for the length of one batch")
			if rowsPerSec > 0 {
				est.BackfillSeconds = float64(have.Rows) / rowsPerSec
			}
		}
		p.Estimates = append(p.Estimates, est)
	}

	if len(problems) > 0 {
		return p, &Error{Problems: problems}
	}
	if len(p.Operations) == 0 {
		return p, &Error{Problems: []string{"the live schema already matches; nothing to migrate"}}
	}
	sort.SliceStable(p.Steps, func(i, j int) bool { return phaseOrder[p.Steps[i].Phase] < phaseOrder[p.Steps[j].Phase] })
	return p, nil
}

// renameIdent replaces whole-word occurrences of an identifier in a SQL expression.
// ponytail: word-boundary replace, not a SQL parser; it would also rewrite the word inside a string literal.
func renameIdent(expr, from, to string) string {
	if from == to {
		return expr
	}
	isWord := func(r byte) bool {
		return r == '_' || r >= '0' && r <= '9' || r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z'
	}
	var b strings.Builder
	for i := 0; i < len(expr); {
		j := i
		for j < len(expr) && isWord(expr[j]) {
			j++
		}
		switch {
		case j == i:
			b.WriteByte(expr[i])
			j = i + 1
		case expr[i:j] == from:
			b.WriteString(to)
		default:
			b.WriteString(expr[i:j])
		}
		i = j
	}
	return b.String()
}
