# Decisions

What was chosen, what was rejected, and why. Newest concerns last. Numbers quoted here are measured
and live in [docs/evidence](docs/evidence/).

## 1. A percentile claim is an exact count over thresholds a sketch proposed

**The problem.** "Top 5%" has to be true for every user, and computing it must not mean ranking the
user base. A quantile sketch gives fast approximate percentiles, but an approximate percentile turned
into a claim is exactly how a 5.2% user gets told "top 5%".

**Chosen.** Use the sketch for what it is good at and nothing else:

1. One t-digest per metric (`approx_quantile`), built in a single streaming pass, proposes about 100
   threshold values.
2. One hash aggregation counts exactly how many users fall between consecutive thresholds; a running
   sum over those ~100 rows gives the exact number of users at or above each threshold.
3. A user's standing is the highest threshold they clear. The claim is the smallest rung of a fixed
   ladder (0.1, 0.5, 1, 2, 3, 5, 10, 15, 20, 25 percent) that `users_at_or_above / population` fits
   under, compared in integers.

Sketch error can only move where a threshold sits, which makes a claim slightly coarser. It cannot
make one false. No step sorts the population; the per-user step is a lookup in a 100-element list.

**Rejected.**
- *Exact `percent_rank()` window per metric.* Correct, and a full sort of the user base per metric.
  It is what the test suite uses to check the pipeline, and what the pipeline exists to avoid.
- *Sketch percentile with a safety margin.* A margin wide enough to be safe throws away true claims;
  one narrow enough to be useful is a probabilistic promise. The requirement is not probabilistic.
- *A sketch with a guaranteed rank error (KLL).* Bounds the error instead of removing it, and DuckDB
  does not ship one; it would have been a new dependency to still not be exact.

## 2. The audit recomputes everything the expensive way

Every run is checked before it can be published: each payload is rebuilt and compared byte for byte,
each number in a card is compared with the warehouse column it is named after, and each "top X%" is
checked against an exact rank from a full sort. `run-all` refuses to publish on any violation. The
same exact check is a dbt test, so it also gates the marts. A test plants a selector that rounds one
rung in the user's favour and asserts the audit catches it.

Rejected: trusting the construction in decision 1 without checking it. The construction is why the
audit passes; the audit is why anyone should believe that.

## 3. Privacy: k-anonymity on thresholds, and nothing about anyone else in a payload

- A threshold fewer than `k` users clear (default 10) is not published. A user who would have sat
  alone above it falls to the next threshold down, which is still true of them.
- A payload contains the user's own numbers, a ladder rung, and the population size. It never
  contains a threshold value, a group size, a rank position, or another account's name.
  (A repository name such as `torvalds/linux` can appear as the user's own "home base". That is
  where *they* were active, in a public feed, not a statement about its owner.)
- "You are #1" was rejected as a card: it tells the reader that everyone else has less than the
  number printed next to it.
- No API route takes a user id. `/v1/wrapped` answers for the user the signed token names.
- A share is a stored snapshot of one card. The public share routes read that row only.

## 4. Automation is not part of the population

In the real hours profiled, 20% (March) and 31% (November) of events came from accounts ending in
`[bot]`; `github-actions[bot]` alone was 12% and 23%. Each hour also had accounts with no bot label
emitting thousands of events, which no person does by hand. A percentile against that population is meaningless, so
accounts are excluded when the login ends in `[bot]` or when any single UTC day exceeds 3,000 events.

Limitation, stated rather than hidden: low-volume unlabelled automation stays in. The generator
includes two such accounts on purpose; at small scale they are not caught.

## 5. Pushes, not commits; UTC, not "night owl"

- `PushEvent.payload` carried `size` and `distinct_size` in the March 2025 hour and did not in the
  November 2025 hour. A "commits this year" number would silently be a "commits until October"
  number. The pipeline counts pushes.
- The feed has no timezone for a user. "You are a night owl" would be a guess presented as a fact.
  The card says "your busiest hour was 14:00 to 15:00 UTC", which is true.
- Shares of a user's own activity ("41% on weekends") are truncated, never rounded up.
- The archetype for a user with no comparison is "The Minimalist" or "The Steady Hand". An earlier
  draft said "The Newcomer"; one year of data cannot know that.

## 6. Low-activity users get a different story, not a smaller one

43% of the generated population has fewer than five events. Three tiers (`minimal`, `light`, `full`)
change which cards are even offered. A minimal user is never shown a bare count of how little
happened, a streak of one, or a comparison they would lose. They are offered where their year began,
where it happened, what they did, and the month it happened in. If one of their few actions is
rare in the population (publishing a release, say), they get that as a real superlative.

Rejected: a single "you were here" fallback card. It is the sad empty card with better copy.

## 7. Selection: rarity as the score, one card per family, per-user tie-breaking

A comparison card scores `log2(population / users_at_or_above)`: bits of surprise. Each user's
rarest true facts win, which is also what makes stories differ between users. At most one card per
family (two for "craft") stops a heavy user getting five variations of "you did a lot". Cards with
no comparison have fixed scores per tier plus a deterministic per-user jitter.

The first version gave 39.5% of users the same set of cards, all of them quiet users receiving
"first event, home base, community". Offering "what you did" cards to quiet users and widening the
jitter brought the most common story down to 11.5% on the same data. A data test fails the build
above 25%.

Rejected: enforcing global quotas per card. It would mean withholding a user's best true card to hit
a distribution target.

## 8. Synthetic year, real shape, same code path

A real year of GH Archive is about 1.3 TB compressed (8,760 files; the March 2025 hour profiled is
153 MB). So the demonstration year is generated, in GH Archive's file naming and JSON shape, and
ingest cannot tell the difference: both are `*.json.gz` in the raw directory. Real hours are fetched
with `wrapped fetch` and go through the identical path; the real-data run is in
[docs/data-profile.md](docs/data-profile.md).

What is calibrated on the real hours: event-type mix, bot share, the one-event-per-actor majority,
the payload drift, a new event type appearing late. What is assumed: the distribution of a user's
events across a year, which three hours cannot show.

Defects: the profiled hours had none of the classic ones (no malformed lines, no duplicates within
an hour, no nulls). The generator plants them anyway, at stated rates and counted in a ledger,
because a year-long feed has them and the pipeline must not learn it the hard way. The two defects
that did come from reality are both handled and tested: hour suffixes are not zero-padded, and a
missing hour returns an error document with HTTP 200 through a naive download. My own interrupted
download produced a truncated gzip; that became the test case for file-level rejection.

## 9. Ingest stages each batch into one file

Reading 8,735 small gzip files directly in DuckDB took 308 s on this machine. Two causes, found by
profiling: binding thousands of Python parameters is slow in DuckDB (each value triggers a failed
`pandas` import probe), and each small file costs several milliseconds to open. The batch is now
decompressed by Python into one text file with a file-index prefix per line, and file metadata goes
in as a single JSON parameter: 21 s. Reading lines as text rather than as JSON is what lets a
malformed line be quarantined with its content instead of vanishing.

## 10. Late data: rebuild the day, do not append the rows

Incremental models find the UTC days touched by batches newer than what they hold and rebuild those
days whole (`delete+insert` on the date). Appending new rows would count a redelivered event twice
whenever its copies arrive in different batches. A test delivers one new and one already-seen event
a week into the next year and asserts the incremental result equals a full refresh.

## 11. Publishing is a pointer flip, so rollback is too

Payloads are keyed by run. A run id is a hash of what the run was built from (raw file manifest,
catalogue version, k), so rebuilding the same inputs is a no-op. Load and activation happen in one
transaction; the previous run stays loaded; rollback swaps two columns of one row. Rejected:
overwriting payloads in place, which has no rollback and exposes half-loaded state to readers.

Rollback restores what is *served*. It does not rewind the DuckDB warehouse, which is derived data
and is rebuilt from the immutable raw files.

## 12. Stack substitutions and things used as-is

- **dbt-duckdb** for all modelling; Python only for I/O (ingest staging, payload text, COPY).
- **Alembic with hand-written SQL**, not an ORM. There are seven tables and the constraints are the
  point; autogenerate would add a model layer to keep in sync with nothing.
- **HMAC-signed tokens from the standard library** instead of a JWT dependency. One issuer, one
  audience, three claims.
- **Pillow** for share cards instead of a headless browser: 60 kB of PNG without shipping Chromium in
  the API image. The cost is that the card layout is written twice (CSS and Pillow).
- **Native `<dialog>`** for the share sheet: focus trap, Escape, and focus return come from the
  platform.
- **CSS animation** for the story progress bar (pause and resume are `animation-play-state`);
  Framer Motion for the paced reveals, card transitions and the count-up.
- **nginx stands in for the CDN** in the compose stack: hashed assets cached forever, share cards
  cached by their versioned URL and served stale if the origin is down.

## 13. Deliberately not built

- **A cloud deployment.** No cloud credentials were available to this build. The deployment that
  exists is the compose stack; CI brings it up from a clean clone, verifies it from outside, drives
  it through a browser and performs the rollback drill. See the limitations in the README.
- **Rate limiting.** Belongs at the edge; noted as a gap.
- **Pre-rendering every user's share cards.** Rendering costs tens of milliseconds and most cards
  are never shared; they are rendered at share time and cached by URL.
- **Per-user timezone inference**, **an LLM writing the copy** (unverifiable text is the opposite of
  the brief), **email delivery of links** (`wrapped links` prints what would be sent).
- **Horizontal scaling of the batch.** See "what breaks first" in the README.
