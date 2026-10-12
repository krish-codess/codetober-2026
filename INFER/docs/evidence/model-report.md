# Model report

Written by `python scripts/report.py` from the seeded database. Do not edit by hand.

Qini is the area between a model's Qini curve and random targeting, in outcome per treated unit, shown x100.
Intervals are 95% from a paired bootstrap of the test split. Uplift is in percentage points of visit rate.

## synthetic

- 198,436 units; split `temporal` into 119,062 train / 39,687 validation / 39,687 test
- Randomization: 0.6672 treated against a design of 0.6667 (sample-ratio p = 0.61); largest standardized mean difference 0.0077; features predict treatment with AUC 0.501
- Average effect on the test split: +6.05 pts (+5.36 to +6.74)
- Champion (best validation Qini among uplift learners): **x_learner**
- Code `e801d06`, data version `6c20e0ce7c79`, seed 7

| learner | val Qini | test Qini | 95% interval | beats `response` | uplift top 10% | top 20% | top 30% | calibration slope | share of oracle | PEHE |
|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| x_learner (champion) | 1.364 | 0.971 | 0.755 to 1.188 | 100% | +12.2 | +10.9 | +10.4 | 0.95 | 98% | 0.0167 |
| t_learner | 1.229 | 0.901 | 0.665 to 1.095 | 100% | +11.7 | +10.0 | +9.8 | 0.81 | 91% | 0.0210 |
| s_learner | 1.094 | 0.890 | 0.672 to 1.099 | 100% | +10.0 | +10.0 | +9.8 | 1.08 | 90% | 0.0271 |
| causal_forest | 1.241 | 0.921 | 0.703 to 1.124 | 100% | +11.8 | +10.7 | +10.1 | 0.81 | 93% | 0.0174 |
| response | -0.000 | 0.015 | -0.224 to 0.215 | - | +4.1 | +5.7 | +6.1 | - | 1% | - |
| random | 0.066 | 0.071 | -0.132 to 0.256 | 66% | +6.5 | +6.3 | +6.4 | - | 7% | - |

Deciles of the champion's predicted uplift, test split (1 = treat first):

| decile | predicted | observed | 95% interval | true |
|---:|---:|---:|---|---:|
| 1 | +12.6 | +12.2 | +9.7 to +14.6 | +13.0 |
| 2 | +10.2 | +9.7 | +7.7 to +11.7 | +10.3 |
| 3 | +9.2 | +9.2 | +7.4 to +11.1 | +8.5 |
| 4 | +7.9 | +6.6 | +4.1 to +9.2 | +8.1 |
| 5 | +6.3 | +6.3 | +4.0 to +8.5 | +6.3 |
| 6 | +5.7 | +4.3 | +2.5 to +6.0 | +4.7 |
| 7 | +5.2 | +4.1 | +2.5 to +5.7 | +4.6 |
| 8 | +4.8 | +4.9 | +3.0 to +6.8 | +4.4 |
| 9 | +3.8 | +4.5 | +2.2 to +6.8 | +4.6 |
| 10 | -2.2 | -2.1 | -4.8 to +0.6 | -3.4 |

Segment scan (model-free, whole experiment): 217 segments tested, 206 help, 1 hurt, 10 unclear after correction.

| segment it hurts | uplift | 95% interval | adjusted p | treated / control |
|---|---:|---|---:|---:|
| history = $350 and over AND channel = Phone | -5.4 | -6.7 to -4.0 | 8.2e-15 | 10,855 / 5,315 |

Units the champion predicts are harmed (test split): 3,058 (7.7%); observed uplift -2.5 pts (-5.6 to +0.5), verdict `unclear`; true effect -4.9 pts. Traits: history = $350 and over (99% vs 19% overall); channel = Phone (95% vs 43% overall); history = $100-$350 (1% vs 46% overall).

Budgeted policies (value 20 per visit, cost 1 per treatment), valued from held-out experiment outcomes, profit per 1,000 customers:

| budget share | treated | policy | treat all | treat none | policy - all (95%) | policy - none (95%) | effect captured | true visits: policy / oracle / all |
|---:|---:|---:|---:|---:|---|---|---:|---|
| 10% | 3,969 (10%) | 2,234 | 2,301 | 2,091 | -68 (-194 to +61) | +143 (+95 to +186) | 20% | 13.0 / 13.2 / 61.0 |
| 20% | 7,937 (20%) | 2,329 | 2,301 | 2,091 | +28 (-88 to +151) | +238 (+174 to +293) | 36% | 23.3 / 23.7 / 61.0 |
| 30% | 11,906 (30%) | 2,414 | 2,301 | 2,091 | +113 (+5 to +236) | +323 (+250 to +385) | 51% | 31.7 / 32.2 / 61.0 |
| 50% | 19,844 (50%) | 2,474 | 2,301 | 2,091 | +173 (+84 to +281) | +383 (+288 to +480) | 73% | 46.2 / 46.9 / 61.0 |
| 100% | 27,636 (70%) | 2,451 | 2,301 | 2,091 | +150 (+73 to +233) | +360 (+254 to +469) | 87% | 55.3 / 55.7 / 61.0 |

With a budget share of 100% the policy still stops where predicted uplift x 20 falls to the cost of 1.

## hillstrom

- 64,000 units; split `hash` into 38,490 train / 12,778 validation / 12,732 test
- Randomization: 0.6671 treated against a design of 0.6667 (sample-ratio p = 0.82); largest standardized mean difference 0.0088; features predict treatment with AUC 0.504
- Average effect on the test split: +5.89 pts (+4.66 to +7.13)
- Champion (best validation Qini among uplift learners): **s_learner**
- Code `e801d06`, data version `43943d234eb2`, seed 7

| learner | val Qini | test Qini | 95% interval | beats `response` | uplift top 10% | top 20% | top 30% | calibration slope |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| x_learner | 0.432 | 0.297 | -0.026 to 0.668 | 78% | +8.7 | +7.6 | +7.4 | 0.67 |
| t_learner | 0.325 | 0.087 | -0.239 to 0.457 | 42% | +8.4 | +7.1 | +6.5 | 0.14 |
| s_learner (champion) | 0.510 | 0.370 | 0.008 to 0.735 | 88% | +8.1 | +7.5 | +6.8 | 0.60 |
| causal_forest | 0.493 | 0.204 | -0.142 to 0.562 | 62% | +6.5 | +7.1 | +6.8 | 0.43 |
| response | 0.129 | 0.151 | -0.174 to 0.523 | - | +9.8 | +8.3 | +6.1 | - |
| random | 0.031 | -0.002 | -0.350 to 0.362 | 30% | +6.7 | +6.4 | +5.5 | - |

Deciles of the champion's predicted uplift, test split (1 = treat first):

| decile | predicted | observed | 95% interval |
|---:|---:|---:|---|
| 1 | +9.6 | +8.1 | +3.4 to +12.7 |
| 2 | +7.8 | +6.6 | +2.6 to +10.7 |
| 3 | +6.9 | +5.6 | +1.4 to +9.9 |
| 4 | +6.2 | +9.9 | +6.1 to +13.6 |
| 5 | +5.8 | +6.6 | +2.9 to +10.2 |
| 6 | +5.3 | +5.9 | +2.2 to +9.5 |
| 7 | +4.8 | +3.8 | -0.1 to +7.6 |
| 8 | +4.3 | +3.9 | +0.2 to +7.6 |
| 9 | +3.5 | +3.4 | -0.5 to +7.4 |
| 10 | +2.2 | +5.1 | +1.8 to +8.3 |

Segment scan (model-free, whole experiment): 170 segments tested, 170 help, 0 hurt, 0 unclear after correction.

No segment is reliably harmed. The lowest observed uplift is +2.9 pts (+0.3 to +5.5) in `womens = no AND channel = Multichannel`, verdict `helps`.

The champion predicts harm for too few held-out units to profile (fewer than 100).

Budgeted policies (value 20 per visit, cost 1 per treatment), valued from held-out experiment outcomes, profit per 1,000 customers:

| budget share | treated | policy | treat all | treat none | policy - all (95%) | policy - none (95%) | effect captured |
|---:|---:|---:|---:|---:|---|---|---:|
| 10% | 1,273 (10%) | 2,273 | 2,388 | 2,209 | -115 (-338 to +110) | +64 (-28 to +140) | 14% |
| 20% | 2,546 (20%) | 2,310 | 2,388 | 2,209 | -78 (-281 to +129) | +101 (-29 to +202) | 26% |
| 30% | 3,820 (30%) | 2,316 | 2,388 | 2,209 | -73 (-243 to +125) | +106 (-36 to +252) | 34% |
| 50% | 6,366 (50%) | 2,445 | 2,388 | 2,209 | +57 (-102 to +225) | +236 (+46 to +419) | 62% |
| 100% | 7,850 (62%) | 2,459 | 2,388 | 2,209 | +71 (-65 to +219) | +250 (+59 to +452) | 74% |

With a budget share of 100% the policy still stops where predicted uplift x 20 falls to the cost of 1.
