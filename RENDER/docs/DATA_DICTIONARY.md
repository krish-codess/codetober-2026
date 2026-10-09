# Data dictionary

Everything the pipeline reads or writes, in the order it is produced. Paths are relative to
`DOTS_DATA_DIR` (default `%LOCALAPPDATA%\render-dots`, or `~/.cache/render-dots`).
"Nullable: no" means the writer guarantees a value and a test checks it.

## raw/&lt;source&gt;/ : immutable inputs

Written once by `fetch`, never modified. Everything downstream can be rebuilt from this directory
and the seed.

| File | Content |
|---|---|
| `*.txt` | GloVe text format, UTF-8, one row per line: `token f1 f2 ... fD`, single spaces, `\n` line ends. Rows are in descending corpus frequency. Read in file-name order |
| `SOURCE.json` | `source`, `url` (null for generated data), `dim`, `files[] {name, bytes, sha256}`, `fetched_at` (UTC) or `generator {rows, seed}`, and for generated data `planted_defects` (count per defect) |

Sources: `glove-twitter-50` (Pennington, Socher, Manning 2014; 2B tweets; PDDL 1.0) and
`synth-<rows>` (seeded Gaussian mixture, 40 clusters each with a dominant script, written in the
same format with the same defects).

## work/&lt;source&gt;/ : intermediates

### vectors.f32

Raw little-endian float32, `accepted x dim` values, row `i` is the vector of id `i`. No header;
the shape is in `ingest.json`. Not normalised (stages normalise to unit length on load).

### meta.parquet : one row per accepted token

zstd, row groups of 65,536 rows, sorted by `id`.

| Column | Type | Nullable | Meaning |
|---|---|---|---|
| `id` | uint32 | no | 0-based, dense, in order of acceptance. The key used by tiles, projections and `vectors.f32`. Stable when a later raw file is added |
| `token` | string | no | The token exactly as in the source. Unique. Not case-folded or Unicode-normalised (1,374 real tokens are not NFC) |
| `rank` | uint32 | no | 1-based line number in its raw file. For GloVe this is the frequency rank (1 = most frequent). Has gaps where lines were quarantined |
| `source_file` | uint8 | no | Index into `SOURCE.json` `files`. Dropped from the browser copy |
| `n_chars` | uint16 | no | Length of `token` in Unicode code points |
| `script` | dictionary&lt;string&gt; | **yes** | Script of the majority of the token's letters: Latin, Cyrillic, Arabic, Kana, Hangul, Han, Thai, Greek, Hebrew, Devanagari, Other. Null when the token has no letters (2,949 real tokens: numbers, punctuation, emoji) |
| `kind` | dictionary&lt;string&gt; | no | `word`, `hashtag` (starts with #), `mention` (starts with @), `number`, `symbol` (no letter or digit), `placeholder` (`<user>`, `<url>`: inserted by GloVe's tweet preprocessing), `mixed` |

The copy published for the browser (`site/data/<build_id>/meta.parquet`) adds:

| Column | Type | Nullable | Meaning |
|---|---|---|---|
| `cluster` | dictionary&lt;string&gt; | no | k-means cluster (k=24) of the unit-normalised vector, labelled `cNN: ` plus the cluster's three most frequent tokens |

### quarantine.parquet : one row per rejected line

`accepted + quarantined = lines read`, asserted on every run.

| Column | Type | Nullable | Meaning |
|---|---|---|---|
| `source_file` | uint8 | no | Index into `SOURCE.json` `files` |
| `line` | uint32 | no | 1-based line number in that file |
| `reason` | string | no | See below |
| `head` | string | no | First 80 bytes of the line, undecodable bytes replaced |

| Reason | Rule |
|---|---|
| `bad_encoding` | Not valid UTF-8 |
| `wrong_field_count` | Splitting from the right on single spaces does not give `dim` numbers and a token |
| `empty_token` | Token is empty or only whitespace/control characters (real file: one token is U+0085) |
| `bad_number` | A component does not parse as a number |
| `non_finite` | A component is NaN or infinite |
| `zero_vector` | All components are zero: no direction, so no cosine neighbours |
| `duplicate_token` | Token already accepted. First occurrence wins (real file: `<unk>` appears three times) |

### Other intermediates

| File | Content |
|---|---|
| `ingest.json` | `lines`, `accepted`, `quarantined`, `quarantine_reasons`, `dim`, `seconds`, `rows_per_s`, and the raw file list it was built from (the idempotency key) |
| `profile.json` | Nulls, cardinality and distributions of every column and of the vectors (norm percentiles, per-dimension spread), plus assumption checks (whitespace in tokens, case collisions, NFC) |
| `cluster.npy` | uint8 per id: k-means label + 1 (0 is reserved for "no value") |
| `proj/<variant>.npy` | float32 `(accepted, 2)`: position of each id, in arbitrary projection units. May contain NaN for a row the projection could not place; such rows are left out of that variant's tiles and counted |
| `proj/<variant>.json` | Parameters, seed, landmark count, seconds per phase, rows per second |
| `eval.json` | Evaluation result (see README, "Projection quality") |
| `experiments.jsonl` | One line appended per evaluation run: time, git commit, seed, raw file hashes, full result |

## site/data/ : what is published

| File | Mutable | Content |
|---|---|---|
| `manifest.json` | **yes** (the only one) | Names the current build and describes it. Schema: `web/src/lib/types.ts` `Manifest` |
| `manifest.previous.json` | yes | The manifest that was current before the last switch; `python -m dots rollback` swaps the two |
| `<build_id>/meta.parquet` | no | Browser copy of the metadata, loaded into DuckDB-WASM as table `meta` |
| `<build_id>/<variant>/<z>-<x>-<y>.bin` | no | One tile |
| `<build_id>/<variant>/density.u32` | no | 256 x 256 little-endian uint32 point counts over the root square, row-major from the lower-left: `grid[j * 256 + i]` is column `i`, row `j` |

`build_id` is the first 12 hex digits of the SHA-256 of the raw file hashes, every projection's
parameters, the tile capacity and the format version.

### Tile format (version 1)

Little-endian, `16 + 12n` bytes.

| Offset | Type | Meaning |
|---|---|---|
| 0 | 4 bytes | `DOTS` |
| 4 | uint16 | Format version, 1 |
| 6 | uint16 | Bytes of category codes per point, 4 |
| 8 | uint32 | `n`, points in the tile |
| 12 | uint32 | Reserved, 0 |
| 16 | uint32[n] | Row `id` |
| 16+4n | uint16[2n] | `x, y` interleaved, quantised to the tile's own square: world = origin + q x side / 65535 |
| 16+8n | uint8[4n] | Category codes per point: script, kind, cluster, 0. Index into the manifest column's `labels`; 0 = "(none)"; 1.. in order of falling frequency |

Tile `z-x-y` covers the square at `(x0 + x * s, y0 + y * s)` with side `s = size / 2^z`, where
`(x0, y0, size)` is the variant's `bounds`. Units are the projection's own (unitless). Points
inside a tile are sorted by the Morton code of `(qx, qy)` (x in the even bits).

### Manifest fields worth knowing

| Field | Meaning |
|---|---|
| `source.synthetic` | True when the data came from the generator; the page shows a badge |
| `columns[].labels`, `.counts` | Legend for each category code, and points per code over the whole dataset |
| `variants[].bounds` | `[x0, y0, size]` of the root tile |
| `variants[].view` | `[x0, y0, x1, y1]` holding the 0.5th to 99.5th percentile of points: the opening view |
| `variants[].tiles` | `[z, x, y, n]` for every tile |
| `variants[].dropped_non_finite` | Rows the projection could not place |
| `variants[].metrics` | That variant's entry from `eval.json`; null if not evaluated |
