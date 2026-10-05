# Index justification

Produced by `python -m parse_app.explain` against the seeded database (56k feedback rows, 158k
label rows, 10k prediction rows). For each index the real query is run with
`EXPLAIN (ANALYZE, BUFFERS)`, then again inside a transaction that drops the index and rolls back.
Full plans are in the `.txt` files next to this one.

| Index | Query it serves | With | Without | Kept |
|---|---|---|---|---|
| `predictions_queue (priority DESC, feedback_id)` | labelling queue page, on every screen of labelling | 3.4 ms | 34.0 ms | yes |
| `labels_node (node_id)` | every label on a node: merge / split / retire, per-node counts | 1.1 ms | 8.1 ms | yes |
| `labels_review (feedback_id) WHERE review_reason IS NOT NULL` | targeted-relabel queue | 0.10 ms | 7.4 ms | yes |
| `feedback_text_sha256 (text_sha256)` | duplicate lookup by text hash | 2.9 ms | 9.0 ms | yes |
| `jobs_queued` | worker claim | 0.07 ms | 0.11 ms | **dropped in 0002** |
| `taxonomy_nodes_parent` | children of a node (trigger path) | 0.05 ms | 0.05 ms | **dropped in 0002** |

Unique and primary-key indexes are constraints and are not listed.

Two things this exercise changed:

- The first version of the queue query never used `predictions_queue`: an anti-join against
  `annotations` made the planner scan and sort all 10k rows (76 ms). The fix was to the data
  model, not the index: a prediction row is deleted when its item is annotated, so the query is
  an ordered index walk with a `LIMIT`.
- The two dropped indexes were on tables of a handful and 241 rows. They cost writes and bought
  nothing; the `0002` downgrade recreates them if those tables ever grow.
