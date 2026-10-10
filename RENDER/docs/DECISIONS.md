# Decisions

What was chosen, what was rejected, and why. Numbers quoted here were measured in this repository;
the records are under `results/`.

## Data

**D1. Dataset: GloVe-Twitter, 50-d, 1,193,514 tokens.** It is a real embedding table of more than a
million rows with something to say about each row (the token), and it is messy in the ways real
vocabularies are: 52% of tokens are not ASCII, eleven scripts, hashtags, placeholders, emoji.
*Rejected:* fastText wiki-news 1M (999,994 rows: short of a million); ANN-benchmark HDF5 files
(vectors only, nothing to filter or summarise); OpenAI/Cohere Wikipedia embeddings (3 to 7 GB to
download for one million rows).

**D2. Download one member of the archive, not the archive.** The 50-d file is 199 MB of a 1.5 GB
zip. `fetch` reads the zip's central directory over HTTP range requests and streams only that
member (78 s here); `zipfile` checks its CRC. Mirrors are tried in order, 5xx and network errors
are retried with capped exponential backoff, 4xx is not retried.

**D3. A generator with the same defects, consumed through the same path.** `synth-<rows>` writes
the same text format and plants seven defects at 0.1% each (bad encoding, wrong field count,
unparseable number, non-finite value, zero vector, empty token, duplicate token). CI and
`docker compose` use it. A test asserts the quarantine counts equal the planted counts exactly.
`DOTS_SOURCE=auto` falls back to it when the real source is unreachable, and the manifest says so:
the page shows a "synthetic data" badge rather than passing generated points off as real.

**D4. Validate at the boundary, quarantine, never drop.** Every line is accepted or written to
`quarantine.parquet` with a reason; `accepted + quarantined = lines` is asserted on every run. The
real file quarantines 3 of 1,193,517 lines: a token that is the single control character U+0085,
and two repeats of `<unk>`. Lines are split from the right, because a token may contain spaces.

**D5. Ids are assigned in order of acceptance** over raw files in name order, so a file that
arrives later (`part-0002.txt`) extends the id space and never renumbers earlier rows (tested).
*Not built:* incremental projection of late rows. A new file changes the build id and the
projection stage reruns; the fitted UMAP model is not persisted.

**D6. Derived columns, because the source has only a token.** `script` (majority Unicode script of
the letters; null when there are none), `kind` (word, hashtag, number, ...) and `cluster` (k-means,
k=24, on the unit vectors). They exist to give selections something to be summarised by and to
give "neighbourhood purity" labels. `cluster` is computed from the same vectors that are projected,
so its purity measures how well a projection keeps high-dimensional structure together; it is not
an independent ground truth, and the docs do not present it as one.

## Modelling

**M1. Baseline first: PCA.** Two principal components of the unit vectors, 1.9 s for 1.19M rows.
Every UMAP number is reported next to it and as a paired difference on the same query rows. Below
PCA there is chance: random neighbours.

**M2. UMAP fitted on 100,000 landmark rows, the rest placed with `transform`.** Fitting 1.19M rows
directly was not attempted: the machine this was built on had 0.6 to 2 GB of memory free. The
landmark fit takes 277 s and placing the other 1,093,514 rows 964 s (1,134 rows/s) for
`n_neighbors=15`. This is also how a projection is used once new data arrives, which is why the
evaluation reports fitted and placed rows separately rather than hiding the difference.
*Cost:* a row's true nearest neighbours are usually not among the 100,000 landmarks, which caps
fine-grained recall for every row. On the small synthetic test set placed rows are clearly worse
than fitted ones (recall 0.13 against 0.19); on the real data the difference is within noise
(README, "Projection quality"). The second variant (`n_neighbors=50`) took 837 s + 3,029 s with
other jobs running on the machine.
*Rejected:* t-SNE (no out-of-sample placement); parametric UMAP (a neural network to train and
version for little gain here).

**M3. Reproducible over fast.** A fixed `random_state` makes UMAP single-threaded. The build id is
a hash of inputs and parameters, so "same id" has to mean "same bytes".

**M4. How the evaluation is split.** There is no train/test split in the supervised sense: nothing
is predicted. The split that matches use is fitted vs placed rows, and queries are drawn half from
each. Neighbours are exact on both sides (brute force in 50-d, k-d tree in 2-D); intervals are a
95% bootstrap over queries.

**M5. Leakage audit.** Checked: (1) no label enters any projection; PCA and UMAP see only vectors.
(2) `script` and `kind` are functions of the token string, which the projection never sees.
(3) `cluster` is derived from the vectors, hence the caveat in D6. (4) Queries are excluded from
their own neighbour lists on both sides, including when exact ties put another row first.
(5) Landmarks are chosen by a seeded draw over ids, independent of labels and of frequency rank.

**M6. Not applicable, and why.** Calibration: nothing outputs a probability. Class-balance-aware
metrics: purity is reported next to its chance level (sum of squared class shares), which is what
makes 0.9 purity on a column that is 56% Latin readable.

**M7. Regression gate.** `tests/eval_baseline.json` holds the metrics of the seeded test dataset;
the suite fails if any falls more than 0.05 below it, if UMAP stops beating PCA on recall (paired
interval above zero), or if PCA stops beating chance.

## Tiles and level of detail

**T1. Additive quadtree.** Every point is in exactly one tile: the root holds a random 32,768 of
all points, each child the next 32,768 that fall in its quadrant. Drawing depths 0..d shows an
increasingly dense sample, and nothing is ever downloaded twice.
*Rejected:* replacing tiles (each level a complete resampling: simpler selection, but every zoom
re-downloads); one big buffer (a 14 MB download before the first point, and memory that grows with
the dataset).
*Known cost:* when a budget stops the descent, sparse regions are fully drawn while dense regions
are still a sample, so density contrast is understated until you zoom in. The 1.19M dataset fits
the default budget, so there it is exact.

**T2. 12 bytes per point.** uint32 id, two uint16 coordinates relative to the tile, four bytes of
category codes. Quantising to the tile, not the plane, makes precision grow with depth: one step
is 1/65,535 of the tile. The GPU reads the file's own bytes as vertex attributes.

**T3. Morton order inside a tile = a quadtree with no nodes.** Sorting a tile's points by the
Morton code of their quantised position makes every quadtree cell one contiguous run, found by
binary search. The worker builds nothing and stores nothing extra; a lasso descends this implicit
tree, accepts whole runs for cells inside the outline and tests individual points only in cells
the outline crosses.
*Rejected:* a pointer quadtree or `d3-quadtree` built in the worker (build time and memory per
tile for the same queries); a uniform grid (degenerates on clustered data, which is what
embeddings are).

**T4. Whole levels only.** A view takes depth d only if every visible tile of depth d fits the
point budget. Stopping part-way through a level leaves some tiles visibly denser than their
neighbours.

**T5. Memory bound.** The view may want at most `budget` points (default 2,000,000; 750,000 on a
narrow screen; `?budget=` overrides). Tiles the view no longer wants are evicted, least recently
wanted first, once more than 1.25 x budget points are held. A property test roams a synthetic
5.5-million-point tree and asserts the bound; the committed benchmark does it in the browser.

**T6. One mutable file.** Everything sits under `data/<build_id>/` and is never rewritten;
`manifest.json` names the build. Tiles can be cached forever, a deploy is one small file changing
after the rest is in place, and a rollback is changing it back. The page polls the manifest and
offers a newer build with a banner instead of swapping data under the user.

## Rendering

**R1. Accumulate, then tone-map.** Points are drawn additively into a floating-point target as
(colour x weight, weight) with a Gaussian falloff; a second pass maps
`log(1 + weight) / log(1 + reference)` to lightness and takes the hue from the weighted mean
colour. Alpha blending saturates after a handful of overlapping points, so it cannot tell 10
points per pixel from 1,000; the log of an unclamped count can.
*Rejected:* per-point alpha tuned by zoom (the "naive alpha stacking" the brief rules out);
histogram equalisation (needs a readback or a compute pass per frame).

**R2. The reference density comes from a precomputed 256 x 256 grid,** not from reading the frame
back. The fullest visible cell, converted to points per pixel, sets the top of the ramp. No
`readPixels` in the frame loop.
*Known cost:* inside one grid cell the estimate is an average, so very local peaks clip at the top
of the ramp.

**R3. Instanced triangles, one per point, one draw call per tile.** Zoomed out there are more
points than pixels, so a point is snapped to the pixel it falls in and drawn as a triangle small
enough to cover that pixel's centre and no other: exactly one fragment. Zoomed in, the triangle
grows around a disc with a smooth falloff and the fragment shader trims it. The view centre is
subtracted in float64 on the CPU, so the shader's float32 only holds screen-sized numbers and
deep zoom does not jitter.
*Rejected, by measurement on the 1.19M-point view:* instanced quads (4 vertices and 2 triangles
per point: frames of 19 to 24 ms against 18 to 20 ms); `gl.POINTS` (a quarter of the vertices but
no faster, because ANGLE on Direct3D expands points in a geometry shader); a single triangle
around a full-size square (more fragments to discard: slower).

**R4. Degradation.** No float render target: half-float, then scaled 8-bit with a notice in the
status line. No vertex texture units or no WebGL: a message instead of a blank canvas. Context
lost: a message with a reload button.

**R5. Selection highlight is a per-tile byte buffer** written by the worker and uploaded as-is, so
drawing never depends on the selection's size or shape.

**R6. Colour.** Categorical slots and the sequential blue ramp come from a palette checked with a
validator for colour-vision-deficiency separation, lightness band, chroma and contrast on both
surfaces (light `#fcfcfb`, dark `#1a1a19`), with separate steps for dark mode. Dark passes every
check; in light mode three hues (aqua, yellow, magenta) are below 3:1 against the surface, which
is acceptable only because every category is also named in text. Only the eight largest categories of a column get a hue; the rest
share grey, because a ninth and tenth hue would not be tellable apart. Colour is never the only
carrier: the legend, the selection table and the hover card name every category in text, and
values are printed as numbers next to every bar.

**R7. Where a frame goes, and why 1.19M points sit at the edge of 60 fps on integrated graphics.**
Measured on Intel Iris Xe by switching stages off: nothing drawn, 1.5 ms; every fragment scissored
away, about 10.5 ms (vertex work and triangle setup for 1.19M instances); normal, 15.6 to 19.9 ms.
So about 10 ms is vertices and about 8 ms is rasterising a million one-pixel triangles. What
moved the number: sprite size (at the original 1.8-pixel sprites a frame took 47 to 63 ms, about
24 fps; one-pixel sprites when zoomed out brought it under 20 ms), one triangle instead of two,
and vertex formats the driver does not have to convert (normalised bytes, no constant attribute:
the scissored cost fell from 12 to 13 ms to 10.5 ms).
What did not: the render-target format (float, half-float and 8-bit cost the same, so blending
bandwidth is not the limit), and vertex-shader texture lookups (three per vertex down to at most
one: no change).
*Not built:* thinning the point set while the view is moving. It would hold 60 fps on any GPU,
but by drawing fewer than a million points, which is the opposite of the requirement. The honest
result is in the README: about 60 fps on average, not on every frame, on this GPU.
*What would actually fix it:* pre-aggregated density tiles for levels where points outnumber
pixels (draw a texture, not points), or a compute-shader rasteriser on WebGPU.

## Selection and metadata

**S1. The worker answers the lasso; DuckDB-WASM answers what the tiles cannot.** Counts per
category, purity, entropy and lift come from the category bytes already in the tiles, in the
worker, while the mouse is still down. Token statistics (frequency-rank quartiles, the most
frequent tokens, search, hover lookup) need the metadata table and come from DuckDB-WASM once the
drag ends. Neither involves a server; the end-to-end test asserts zero network requests.
Measured on 1.19M points: counts in 36 to 165 ms, token statistics 236 to 871 ms after that. The
three rank quantiles are one list-valued aggregate (one sort, not three), and a throwaway query
at load pays DuckDB's first-query cost before the user's first lasso.

**S2. One query in flight, latest outline wins.** A drag produces far more outlines than answers
are needed; anything queued behind the in-flight query is replaced by the newest.

**S3. The metadata table is loaded whole into DuckDB-WASM,** after the first frame, as its own
JavaScript chunk, with a real progress bar. *Rejected:* HTTP range reads of the Parquet file per
query: a selection touches ids in every row group, so pushdown has nothing to prune.
*This is the component that breaks first at 10x* (see README).

**S4. A selection is a polygon, not a list.** It is re-evaluated when tiles arrive or the filter
changes, and reported as a partial count while a tile under the outline is not loaded.
*Not built:* carrying a selection across projection variants (it is cleared on switch).

**S5. Exactness is tested, not assumed.** The browser's selection must return the same row ids as
(a) a brute-force scan, for generated points and polygons including self-intersecting ones and
points exactly on edges, and (b) the Python reference API, on a committed fixture that a Python
test regenerates and compares byte for byte. The first run of (a) found a real bug: a rectangle
within rounding distance of an edge was classified as wholly outside, because the classifier's
tolerance did not cover the cancellation error in the point-in-polygon expression.

## Services

**A1. A static site is the product; the API is the reference.** Production is files on a CDN.
The FastAPI service serves those same files, a server-side selection summary and a paginated
point listing, for local development, for clients without WebGL, and as the independent
implementation in S5.

**A2. No authentication.** There are no users, tenants or writes, and the data is public. The
compose file mounts the data read-only into the server and drops all capabilities.

**A3. Parameterised everywhere.** Values reach DuckDB only as bound parameters, prepared-statement
arguments or registered Arrow tables; filter labels are checked against the manifest and mapped to
codes before use. Polygons are capped at 2,048 vertices, pages at 1,000 rows.

**A4. Cursors carry the build id.** A cursor issued before a rebuild is refused with
`stale_cursor` instead of silently skipping or repeating rows.

## Deliberately not built

- GPU picking, WebGL2 or WebGPU paths: the worker index answers hover in well under a frame.
- Text labels on the plot, cluster outlines, animated transitions between variants.
- Server-side tile generation on demand: tiles are a batch product.
- A user-facing upload of new embeddings.
- Multi-GPU or distributed UMAP.
