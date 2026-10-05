package plan

import (
	"context"
	"database/sql"

	"github.com/lib/pq"
)

// Load reads the tables of the public schema from the catalog, ignoring pgroll's own shadow columns.
func Load(ctx context.Context, db *sql.DB) (Schema, error) {
	s := Schema{}
	rows, err := db.QueryContext(ctx, `
		SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
		       ARRAY(SELECT conname::text FROM pg_constraint k
		             WHERE k.conrelid = c.oid AND k.contype = 'c' AND k.conkey = ARRAY[a.attnum] ORDER BY 1),
		       COALESCE(NULLIF(st.n_live_tup, 0), GREATEST(c.reltuples, 0)::bigint), pg_total_relation_size(c.oid)
		FROM pg_class c
		JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
		LEFT JOIN pg_stat_user_tables st ON st.relid = c.oid
		WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND a.attname NOT LIKE '\_pgroll\_%'
		ORDER BY c.relname, a.attnum`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var table string
		var col Column
		var n, bytes int64
		if err := rows.Scan(&table, &col.Name, &col.Type, &col.Nullable, pq.Array(&col.Checks), &n, &bytes); err != nil {
			return nil, err
		}
		t := s[table]
		t.Columns, t.Rows, t.Bytes = append(t.Columns, col), n, bytes
		s[table] = t
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	idx, err := db.QueryContext(ctx, `
		SELECT t.relname, i.relname FROM pg_index x
		JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid
		WHERE t.relnamespace = 'public'::regnamespace AND t.relkind = 'r'
		  AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = x.indexrelid)
		ORDER BY 1, 2`)
	if err != nil {
		return nil, err
	}
	defer idx.Close()
	for idx.Next() {
		var table, name string
		if err := idx.Scan(&table, &name); err != nil {
			return nil, err
		}
		t := s[table]
		t.Indexes = append(t.Indexes, name)
		s[table] = t
	}
	return s, idx.Err()
}
