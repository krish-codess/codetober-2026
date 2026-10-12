# Measured timings

Written by `python scripts/report.py` from the run report of the last `uplift seed`. Do not edit by hand.

Machine: Intel64 Family 6 Model 140 Stepping 2, GenuineIntel, Windows 10.0.26200, Python 3.11.5, PostgreSQL 18 on the same host. One process; the forest uses 2 threads.

| stage | seconds | volume | throughput |
|---|---:|---|---:|
| generate | 29.9 | 205,424 rows written | 6,859 rows/s |
| ingest synthetic | 40.6 | 202,414 rows (6,988 quarantined) | 4,982 rows/s |
| ingest synthetic (late file) | 0.8 | 3,010 rows (0 quarantined) | 3,583 rows/s |
| ingest hillstrom | 11.6 | 64,000 rows (0 quarantined) | 5,532 rows/s |
| train synthetic | 148.7 | 198,436 units, 6 learners | - |
| score synthetic | 45.4 | 198,436 units scored | 4,374 rows/s |
| train hillstrom | 55.1 | 64,000 units, 6 learners | - |
| score hillstrom | 7.3 | 64,000 units scored | 8,791 rows/s |

Inside training:

| dataset | stage | seconds |
|---|---|---:|
| synthetic | fit_total | 91.0 |
| synthetic | fit_causal_forest | 46.8 |
| synthetic | evaluate | 45.1 |
| synthetic | fit_s_learner | 19.6 |
| synthetic | fit_x_learner | 11.1 |
| synthetic | load_and_featurize | 8.2 |
| synthetic | fit_t_learner | 6.9 |
| synthetic | fit_response | 6.6 |
| synthetic | segment_scan | 2.4 |
| synthetic | audit | 1.6 |
| synthetic | persist | 0.3 |
| synthetic | fit_random | 0.1 |
| hillstrom | fit_total | 36.4 |
| hillstrom | fit_causal_forest | 26.6 |
| hillstrom | evaluate | 14.9 |
| hillstrom | fit_x_learner | 3.4 |
| hillstrom | fit_t_learner | 2.7 |
| hillstrom | fit_s_learner | 2.4 |
| hillstrom | load_and_featurize | 1.9 |
| hillstrom | fit_response | 1.2 |
| hillstrom | audit | 1.1 |
| hillstrom | segment_scan | 0.6 |
| hillstrom | persist | 0.2 |
| hillstrom | fit_random | 0.0 |
