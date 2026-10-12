# Decisions

What was chosen, what was rejected, and why. Newest concerns are not at the top; the order follows the build.

## Data

**Hillstrom, not Criteo-UPLIFT, as the real dataset.** Criteo is 13.9M rows and about 300 MB compressed; the
development machine has 16 GB of RAM with 1 to 2 GB free. Hillstrom is 64,000 rows, a clean randomized design, and
small enough to retrain in two minutes. Rejected: sampling Criteo, because a 1% sample of anonymous features `f0..f11`
would add volume but no segment anyone can read. The ingestion contract is Hillstrom-shaped on purpose; Criteo would
be a second adapter and a second feature list, not a parameter.

**Binary treatment: any e-mail vs none.** The file has two e-mail arms. Modelling "which e-mail" is a
multi-treatment problem with a different policy (assign, not target). Out of scope; the two arms are pooled.

**Default outcome `visit`.** Conversion is 0.9% (578 events); no method can rank on that with 13k held-out rows.
`conversion` and `spend` are accepted by `ingest --outcome`, and a dataset is locked to the one it started with.

**No de-duplication without a key.** Hillstrom has no customer id and 6,562 identical rows. They are loaded as
distinct units. See docs/data-profile.md. Rejected: hashing the row into an id, which would delete real customers.

**Missing feature = NULL and loaded; invalid value = quarantined.** A blank is information the source did not have.
A recency of 99 is a defect in the feed. Treating them the same either loses usable units or loads garbage.
Unknown categories are quarantined rather than nulled, so a new upstream value is noticed.

**A unit seen in both arms is quarantined in every copy.** One of the records is wrong and there is no telling
which; keeping either biases the arm it lands in.

**Late outcomes arrive as a later file.** Rows with no outcome yet are quarantined (`missing_outcome`), and the unit
is loaded when a later file carries it. Rejected: an "outcome pending" unit state with updates in place, which makes
`unit` mutable and every downstream result time-dependent. With insert-only units, a dataset's content is exactly
the set of its raw files, and that set has a hash (`data_version`).

**Typed columns, not a JSONB feature bag.** Seven features with real domains get CHECK constraints the database
enforces. A JSONB bag would take any dataset and validate none.

**Raw files are copied by content hash before parsing; PostgreSQL holds the validated rows.** Any table can be
rebuilt from `raw/<sha256>.csv`. Rejected: storing raw rows in the database too (the quarantine does keep its rows,
because those are the ones someone has to look at).

## Modelling

**T-, S- and X-learner are implemented here; the causal forest is EconML's.** The meta-learners are a dozen lines
each and worth owning. `tests/test_learners.py` holds all three to EconML's implementations at 1e-9 on 15,000
held-out predictions, with the same base model and seed. That differential test is the proof of the "hard core".
Rejected: writing a causal forest (honest splitting and local centering are not a weekend), and using EconML for
everything (nothing would be proven, only called).

**One base model everywhere: shallow, regularised `HistGradientBoostingRegressor`.** Depth 3, 120 trees, 200-row
leaves. Effects are a few points on a 10% base rate; a flexible model fits noise per arm and the difference of two
noisy fits is noise. A regressor on a 0/1 outcome estimates the same conditional mean as a classifier and serves
binary and continuous outcomes and the X-learner's second stage. Not tuned: tuning on uplift needs its own
validation split and the data does not have the rows to spare.

**Propensity is the treated share, not a fitted model.** Assignment was randomized, so the propensity is known. A
fitted propensity model would add variance to estimate a constant. (EconML gets a `DummyClassifier` for the same
reason.) This is also why the system refuses non-randomized data instead of adjusting for it.

**Two baselines, and one of them is the point.** `random`, and `response`: P(visit | features), "target the likely
buyers". On the synthetic data the response model earns 1.5% of the achievable Qini, the same as random, because the
customers most likely to buy are the ones the e-mail changes least.

**Three-way split; the champion is picked on validation and reported on test.** Selecting on the split you report
is how a 6-way comparison manufactures a winner. Temporal when units have `assigned_at`, hash of the unit id when
they do not. Rejected: cross-validation, which gives tighter numbers but cannot be temporal and makes "the model that
is served" five models.

**Uncertainty by paired bootstrap.** All learners are scored on the same resamples of the test split, so "X beats
the baseline in 97% of resamples" is a paired statement. 200 rounds; the interval is on the evaluation, not on
retraining, so it understates total uncertainty (see limitations).

**Qini ties are broken at random, seeded.** A stable sort would rank tied scores in input order, and input order is
often time or id. A model that scores everyone the same must look like random targeting.

**Curves are normalised to end at the average treatment effect.** Every model's curve on the same data shares both
endpoints. The Qini coefficient is the area above the random line in those units (outcome per treated unit), so it is
comparable across models on one dataset and not across datasets.

**Training refuses when randomization looks broken.** A sample-ratio test (p < 0.001) or features that predict
treatment (AUC > 0.55, which is also what a post-treatment feature leaking in looks like) fails the experiment with
the reason recorded. Every estimator here is only unbiased under randomization.

**"Hurts" has to be earned.** The segment scan calls a segment harmful only if its Benjamini-Hochberg adjusted
p-value is under 5% across every segment tested. The model's predicted-harm group is judged on held-out outcomes,
and the verdict is `unclear` when its interval crosses zero. On Hillstrom the answer is "no harmed segment found",
and the UI says that is a finding.

**The policy is evaluated from experiment outcomes, never from predictions.** Targeted units contribute the mean
outcome of targeted-and-treated units; the rest, of untargeted-and-control units. A model cannot grade itself.

**The policy population is the test split.** It stands in for the next campaign's audience: units the model did not
train on and was not selected on. Scores for all units are stored, but in-sample scores are over-confident and a
policy that mixed them would favour training units.

**Greedy top-n is the exact optimum.** With one cost per unit, the budgeted problem is a knapsack with equal
weights. If costs varied by unit this would become a real knapsack; it does not, so no solver.

**Budget as a database CHECK.** `n_targeted * cost_per_treatment <= budget` is enforced by PostgreSQL as well as by
a property test over 500 generated cases.

## Storage and serving

**Scores only for the champion.** Scoring all six learners for 200k units is 1.2M rows nobody reads. Curves and
deciles for every learner are computed in memory at training time and stored small.

**A policy stores its definition, not its members.** `(run, n_targeted)` plus the fixed ordering regenerates the
list exactly, so there is no `policy_unit` table to keep in step.

**Rollback is `retire`.** The API serves the latest `complete` experiment; retiring one makes the previous one
current again, with its scores and artifacts still in place. Rejected: an "active experiment" pointer table, which is
one more thing that can disagree with reality.

**Artifacts are verified before unpickling.** A model file is a pickle. The API and the scorer load one only if its
SHA-256 matches what training recorded in the database, and the API mounts the artifact volume read-only.

**Two bearer tokens, compared in constant time.** Read, and admin (create and export policies). There are no users
or tenants in this system, so there is no user table, sessions or OAuth. Each service has its own database user.

**Training is a batch command, not an endpoint.** It runs for minutes. Putting it behind HTTP means a job queue,
status polling and cancellation; a scheduler calling `uplift train` needs none of them.

## Interface

**No chart library.** Two SVG charts, about 250 lines, with exactly the marks needed (crosshair, whiskers, a
predicted-vs-observed overlay). A library would be most of the bundle.

**The hover layer is a readout under the chart, not a floating tooltip.** It is reachable by arrow keys, works by
touch, is announced to screen readers, and cannot cover the lines.

**Four learner colours plus two neutral baselines.** The four are validated for colour-vision deficiency and
contrast in light and dark (`validate_palette.js`); two fall under 3:1 on the light surface, so each series also has
its own dash pattern, a named legend entry, a named readout row and a table row. Baselines are grey because they
are references, not candidates.

**Client cache with a 60-second freshness window.** Results change when a batch job finishes, not second to second.
A failed revalidation keeps the saved copy on screen, marked stale.

## Deliberately not built

- Criteo ingestion; multi-treatment (which e-mail); continuous treatments (how big a discount).
- Hyperparameter search, model registry service, experiment-tracking server. `experiment` and `model_run` rows plus
  hashed artifacts are the record.
- Scheduled retraining, drift monitoring, alert rules. `/metrics` exposes the counters; nothing scrapes them here.
- Rate limiting and per-user accounts.
- A second knapsack solver, a propensity model, and inverse-propensity weighting for observational data.
- Cloud infrastructure. The deployment target is the compose stack; see the README for what that does and does not
  prove.

## Substitutions from the stated stack

None. Python, EconML, scikit-learn, PostgreSQL, FastAPI, React, Docker.
