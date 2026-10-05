package shop

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/lib/pq"
)

const keptAsNull = "unreadable_amount_kept_as_null"

type IngestResult struct {
	File        string `json:"file"`
	SHA256      string `json:"sha256"`
	Skipped     bool   `json:"skipped"` // this exact file was ingested before
	Lines       int    `json:"lines"`
	Accepted    int    `json:"accepted"`
	Quarantined int    `json:"quarantined"`
	Duplicates  int    `json:"duplicates"`
	Late        int    `json:"late"` // more than 7 days behind the newest order seen so far
}

// Ingest loads one raw feed file in a single transaction: validate each line, quarantine what fails,
// insert the rest, skip order_refs already present. A file is identified by its hash, so running it
// again is a no-op, and a crash half way leaves nothing behind.
func Ingest(ctx context.Context, db *sql.DB, appVersion, path string) (IngestResult, error) {
	res := IngestResult{File: filepath.Base(path)}
	data, err := os.ReadFile(path)
	if err != nil {
		return res, err
	}
	sum := sha256.Sum256(data)
	res.SHA256 = hex.EncodeToString(sum[:])

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return res, err
	}
	defer tx.Rollback()
	var claimed string
	err = tx.QueryRowContext(ctx, `INSERT INTO ingest_files (sha256, name) VALUES ($1, $2) ON CONFLICT (sha256) DO NOTHING RETURNING sha256`, res.SHA256, res.File).Scan(&claimed)
	if err == sql.ErrNoRows {
		res.Skipped = true
		return res, tx.QueryRowContext(ctx, `SELECT lines, accepted, quarantined, duplicates, late FROM ingest_files WHERE sha256 = $1`, res.SHA256).
			Scan(&res.Lines, &res.Accepted, &res.Quarantined, &res.Duplicates, &res.Late)
	}
	if err != nil {
		return res, err
	}

	if _, err := tx.ExecContext(ctx, `CREATE TEMP TABLE stage (line_no int, order_ref text, email text, name text, country text,
		status text, amount text, amount_cents bigint, currency text, placed_at timestamptz) ON COMMIT DROP`); err != nil {
		return res, err
	}
	type reject struct {
		line   int
		reason string
		raw    []byte
	}
	var rejects []reject
	stage, err := tx.PrepareContext(ctx, pq.CopyIn("stage", "line_no", "order_ref", "email", "name", "country", "status", "amount", "amount_cents", "currency", "placed_at"))
	if err != nil {
		return res, err
	}
	now := time.Now()
	var newest time.Time
	valid := 0
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	for sc.Scan() {
		res.Lines++
		line := bytes.Clone(sc.Bytes())
		o, reason := Validate(line, now)
		if reason != "" {
			rejects = append(rejects, reject{res.Lines, reason, line})
			continue
		}
		valid++
		if o.PlacedAt.After(newest) {
			newest = o.PlacedAt
		} else if newest.Sub(o.PlacedAt) > 7*24*time.Hour {
			res.Late++ // late arrivals are accepted: nothing downstream assumes feed order
		}
		var cents *int64
		if o.Amount != nil {
			if c, ok := ParseCents(*o.Amount); ok {
				cents = &c
			} else if appVersion != "v1" { // the v2 schema stores cents: the order is kept with a NULL amount, the raw line preserved
				rejects = append(rejects, reject{res.Lines, keptAsNull, line})
			}
		}
		if _, err := stage.ExecContext(ctx, res.Lines, o.Ref, o.Email, o.Name, o.Country, o.Status, o.Amount, cents, o.Currency, o.PlacedAt); err != nil {
			return res, err
		}
	}
	if err := sc.Err(); err != nil {
		return res, err
	}
	if _, err := stage.ExecContext(ctx); err != nil {
		return res, err
	}
	stage.Close()

	amount := "amount"
	if appVersion != "v1" {
		amount = "amount_cents"
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO customers (email, name, country)
		SELECT DISTINCT ON (email) email, name, country FROM stage ORDER BY email, line_no
		ON CONFLICT (email) DO NOTHING`); err != nil {
		return res, err
	}
	ins, err := tx.ExecContext(ctx, fmt.Sprintf(`
		INSERT INTO orders (order_ref, customer_id, status, %[1]s, currency, placed_at)
		SELECT DISTINCT ON (s.order_ref) s.order_ref, c.id, s.status, s.%[1]s, s.currency, s.placed_at
		FROM stage s JOIN customers c USING (email) ORDER BY s.order_ref, s.line_no
		ON CONFLICT (order_ref) DO NOTHING`, amount))
	if err != nil {
		return res, err
	}
	n, _ := ins.RowsAffected()
	res.Accepted = int(n)
	res.Duplicates = valid - res.Accepted

	// COPY cannot target a view, and the application only sees views: stage, then insert.
	if _, err := tx.ExecContext(ctx, `CREATE TEMP TABLE rejects (line_no int, reason text, raw bytea) ON COMMIT DROP`); err != nil {
		return res, err
	}
	q, err := tx.PrepareContext(ctx, pq.CopyIn("rejects", "line_no", "reason", "raw"))
	if err != nil {
		return res, err
	}
	for _, r := range rejects {
		if r.reason != keptAsNull {
			res.Quarantined++
		}
		if _, err := q.ExecContext(ctx, r.line, r.reason, r.raw); err != nil {
			return res, err
		}
	}
	if _, err := q.ExecContext(ctx); err != nil {
		return res, err
	}
	q.Close()
	if _, err := tx.ExecContext(ctx, `INSERT INTO quarantine (source_sha256, line_no, reason, raw) SELECT $1, line_no, reason, raw FROM rejects`, res.SHA256); err != nil {
		return res, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE ingest_files SET lines = $2, accepted = $3, quarantined = $4, duplicates = $5, late = $6 WHERE sha256 = $1`,
		res.SHA256, res.Lines, res.Accepted, res.Quarantined, res.Duplicates, res.Late); err != nil {
		return res, err
	}
	return res, tx.Commit()
}
