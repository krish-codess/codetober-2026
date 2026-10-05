# Data profile

What the real source looked like before any schema was designed, and what the feed looks like after
the envelope and defects are added. Reproduce the feed numbers with
`python -m parse_app.experiments audit` (→ `reports/audit.json`).

## Source: M-ABSA @ `521ab96c`

| | |
|---|---|
| Format | One line per sentence: `text####[[aspect term, category, polarity], …]`, UTF-8 |
| Shape | 7 domains × 21 languages × {train, dev, test}; 12 languages used |
| Size | ≈ 2,100 sentences per domain per language; 14,776 per language; 252 files, 38 MB for 12 languages |
| Parallel | Yes: line *i* of every language file is the same sentence. Verified English vs Swahili: **0** category mismatches in 14,776 lines |
| Licence | Apache-2.0 |

### Values that violated the obvious assumptions

| Assumption | Reality | Handling |
|---|---|---|
| The label is a parseable Python/JSON literal | 52 of 14,776 Swahili lines are not: unescaped apostrophes in the aspect term (`['Dr. Ng's', 'faculty general', 'positive']`) | Regex that reads only the last two fields of each triplet; tested |
| One category format | Three: `rooms comfort` (space), `LAPTOP#GENERAL` (hash, upper case), `Seller Service#Attitude` (hash, spaces, mixed case); one domain (`sight`) is single-level | `taxonomy.canonical_path` → `domain/entity/attribute` |
| Polarity is an enum | `positive`, `POS`, `negative`, `NEG`, `Neg`, `neutral`, `NEU`, `Neu`, `'NEU '`, `conflict` | Not used (sentiment is out of scope) — recorded so nobody trusts it later |
| Categories are aspects | `polarity negative`, `polarity neutral` appear as categories | Dropped, counted as `gold_unmapped` (60 label occurrences) |
| Every sentence has a label | 14% have an empty list (18% of the pool after folding rare nodes) | Kept; labelled with the domain only |
| One label per sentence | 0: 14% · 1: 62% · 2: 17% · 3+: 8% (max 11) | Multi-label model and metrics |
| Categories are well populated | Long tail: of 108 laptop categories, 49 have < 5 training examples | Nodes with < 8 pool examples folded into the parent (290 raw categories → 166 leaves) |
| Texts are unique | 51 exact duplicates within English train; 55 pool sentences identical to a test sentence | `duplicate_of`; excluded from queue and training |
| The domain name describes the domain | `sight` contains comments on maths lecture videos, not sightseeing | Left as published; noted here |
| Text is clean | English is pre-tokenised (`" ."`), some sentences contain `@[USERNAME]`, timestamps (`19:40`), HTML remnants | Whitespace and control characters normalised; nothing else is rewritten |

Length (English, characters): median 76, p95 213, p99 392, max 1,266, min 2.

Categories per domain (raw): laptop 108, phone 91, hotel 34, coursera 30, restaurant 13, food 10, sight 5.

## Feed after the envelope and defect injection (seed 0)

56,685 lines: 11,157 pool records (one language each) + 45,528 test records (3,794 sentences × 12 languages).

Pool language mix (accepted rows): en 3,662 · es 1,383 · fr 1,038 · de 1,002 · zh 721 · ja 712 · ru 548 · ar 432 · *not supplied* 359 · hi 311 · tr 281 · sw 173 · th 160.

| Injected defect | Rate | Outcome at ingest | Count in the seed feed |
|---|---|---|---|
| Truncated line | 0.2% | quarantine `malformed_json` | 23 |
| Invalid UTF-8 byte | 0.2% | quarantine `bad_encoding` | 19 |
| Empty / whitespace text | 0.3% | quarantine `empty_text` | 33 |
| Runaway text (> 4,000 chars) | 0.2% | quarantine `too_long` | 29 |
| `text` field missing | 0.2% | quarantine `missing_text` | 29 |
| Double-decoded UTF-8 (mojibake) | 0.3% | **repaired**, counted | 11 changed (ASCII text is unaffected) |
| Language tag missing | 3% | accepted, `lang` NULL | 359 |
| Language tag as `EN`, `en-XX`, `en_xx` | 3% | accepted, normalised | — |
| Timestamp missing | 1% | accepted, `created_at` NULL | 112 |
| Timestamp as epoch or `YYYY/MM/DD` | 1% | accepted, parsed | — |
| Timestamp in 2099 / unparseable | 0.4% | quarantine `bad_timestamp` | 50 |
| Id missing | 0.5% | accepted, id derived from content | — |
| Late arrival (delivered at the end) | 1% | accepted, `is_late` | 108 |
| Exact redelivery | 1% | quarantine `duplicate_delivery` | 104 |
| Same text, new id | 0.5% | accepted, `duplicate_of` set | 56 |
| Same id, different text | 0.2% | quarantine `conflicting_duplicate` (first version kept) | 26 |

Totals at ingest: 56,372 accepted, 313 quarantined (0.55%), 0 dropped silently.
After de-duplication and the leakage guard: 10,733 usable pool items.

## Labels after mapping to taxonomy v1

241 nodes (7 domains, 68 entities, 166 attributes). Mean 3.1 nodes per item including ancestors.
Median pool support per node: 41. Domain shares of the pool are flat (13–15% each).
