# Benchmark

Measured, not estimated. The raw per-stage reports written by the CLI are next to this file:
[run-report-200k.jsonl](run-report-200k.jsonl), [run-report-real-hours.jsonl](run-report-real-hours.jsonl),
and the generator's defect ledger [generator-truth-200k.json](generator-truth-200k.json).

**Machine.** Windows 11 laptop, 16 GB RAM of which roughly 0.5 to 1.8 GB was free during the runs
(a browser and an editor were open). DuckDB capped at 1 GB and 4 threads. Data on a local SSD,
outside the synced folder. PostgreSQL 18.3 on the same machine with 64 MB of shared buffers.
These are numbers from a constrained machine; read them as a floor.

## Run 1: a generated year, 200,000 users

`wrapped generate --users 200000 --seed 2025`, then `wrapped run-all`.

| | |
|---|---:|
| Raw files | 8,814 hourly `.json.gz`, 241 MB |
| Raw lines | 6,427,250 |
| Accepted / quarantined | 6,419,473 / 7,777 |
| Redelivered events (planted) | 12,905 |
| Late arrivals (planted) | 25,849 |
| People in the population (automation excluded) | 199,956 |
| Rankable (user, metric) values | 1,599,521 |
| Payloads | 199,956 |
| Payload cards | 1,161,386 |
| Comparison claims checked by the audit | 422,831 |
| Audit violations | **0** |

The quarantine reconciles with the ledger exactly: 3,250 missing actor + 1,284 missing id + 1,259
unparseable timestamp + 671 far-future timestamp + 1,313 malformed JSON = 7,777.

| Stage | Budget | Measured | Throughput | Notes |
|---|---:|---:|---:|---|
| generate (test data only) | n/a | 318 s | 20,200 lines/s | Not part of the batch window. |
| ingest | 5 min | 175 s | 36,700 lines/s | 18 batches of 500 files. Second run with nothing new: 0.9 s. |
| transform (`dbt build`, first load) | 5 min | 111 s | 57,900 events/s | 78 nodes: 11 models and their tests, including the exact-rank test. |
| transform (incremental, nothing new) | 2 min | 69 s | | Mostly dbt start-up and re-running the tests. |
| build (payloads) | 5 min | 142 s | 1,410 users/s | One Python process. Re-run on the same inputs: 0.15 s. |
| audit | 5 min | 143 s | 1,400 payloads/s | Rebuilds every payload and re-checks 422,831 claims against exact ranks. |
| publish | 5 min | 238 s | 840 users/s | COPY of 200k payloads and 1.16M card rows in one transaction. |
| **Batch total, ingest to published** | **30 min** | **809 s (13.5 min)** | | |

The target was a half-hour window for a 200,000-user year on this laptop, split into five minutes
per stage. Every stage is inside its budget; publish is the closest to its limit.

### Superlative distribution (Run 1)

Tiers: 47,497 full (23.8%), 66,612 light (33.3%), 85,847 minimal (42.9%).

| Card | Share of users | | Card | Share of users |
|---|---:|---|---|---:|
| first_event | 72.0% | | active_days | 13.8% |
| home_base | 61.3% | | issues_opened | 10.8% |
| pushes | 45.6% | | streak | 10.3% |
| stars | 23.8% | | busiest_day | 9.5% |
| comments | 19.5% | | busiest_month | 9.5% |
| reviews | 18.9% | | forks | 9.4% |
| total_events | 18.2% | | releases | 7.1% |
| community | 16.3% | | weekend | 3.2% |
| explorer | 15.3% | | peak_hour | 2.6% |
| prs_opened | 13.9% | | | |

2,850 distinct sets of cards. The most common set is held by 12.2% of users. All 19 selectable
card types are in use.

## Run 2: real GH Archive hours

`wrapped fetch` of four hours of 2025, then `wrapped run-all --no-publish`.

| | |
|---|---:|
| Files | 3 ingested, 1 rejected (a download cut off at 17 of 69 MB: unreadable gzip, recorded, batch continued) |
| Raw lines | 570,995 |
| Accepted / quarantined | 570,995 / 0 |
| Accounts excluded as automation | 866 (865 `[bot]` logins, 1 over the daily ceiling), 143,205 events: 25% of the feed |
| People in the population | 159,498 |
| Tiers | 183 full, 14,409 light, 144,906 minimal |
| Claims checked by the audit | 192,330 |
| Audit violations | **0** |
| Distinct sets of cards / most common set | 483 / 33.7% |

| Stage | Measured |
|---|---:|
| ingest (238 MB compressed) | 54 s |
| transform | 67 s |
| build | 116 s |
| audit | 109 s |

What this run shows and does not show. It shows the pipeline consuming the real feed unchanged, the
real feed's lines all passing validation, a quarter of real events being excluded as automation, and
every claim made about a real account being true. It does not show a year: three hours is 0.03% of
one, 91% of accounts have fewer than five events in it, and that is why one set of cards reaches a
third of users here against 12% on a full year. Nothing from this run was published or committed
beyond these counts.

## Problems that only appeared at scale

Each of these passed every test on the 3,000-user and 500-user datasets and failed at 200,000.

1. **dbt could not spill to disk.** `temp_directory` was a per-cursor setting in the dbt profile;
   dbt re-applies settings on each new cursor and DuckDB refuses to change the temp directory once a
   query has spilled into it. It is now a database-level option.
2. **The audit ran out of memory.** It joined every payload's text to the warehouse in one query.
   Then, with that removed, two list aggregations and a sort still exceeded 1 GB together. Build and
   audit now read sorted streams and merge them in Python; memory is flat in the number of users.
3. **The pagination query sorted the whole run.** Found by reading its plan, not by a timeout:
   588 ms, now 0.3 ms. See [explain-analyze.md](explain-analyze.md).
4. **The one secondary index was never used.** Same source. Dropped in migration 0003.
