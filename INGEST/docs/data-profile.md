# What the real data looks like, and what it changed

The schema and the validator were written after profiling real AIS traffic, not before. The capture
below is 85,194 raw sentences from an AISHub relay (the `nmea-sample` file distributed with the pyais
project). Reproduce it with `python -m ghostfleet profile <file>`.

The Norwegian Coastal Administration's open feed (`153.44.253.27:5631`) was the intended live source,
but it is not reachable from the network this was built on (TCP connect times out), and AISStream needs
an API key that was not available. Both are supported and configured in `.env.example`; neither was
exercised live. See the limitations section of the README.

## Findings that shaped the design

| Finding | Share | Consequence |
|---|---|---|
| Heading "not available" (511) | 44.5% of positions | `heading` is nullable everywhere; the map draws dots, not oriented ships |
| Stationary vessels (sog < 0.5 kn) | 58.4% | Separate, much longer silence threshold for vessels that were not moving, or half the fleet would be "dark" every night |
| Byte-identical repeated sentences | 3.9% | Deduplication is needed even with a single provider, so it lives in the primary key, not in provider-specific logic |
| Multi-part sentences | 5.7% of sentences | Reassembly with a timeout; fragments are checkpoint-protected across restarts |
| Non-vessel MMSIs (base stations, aids to navigation, aircraft) in position reports | 1.6% | Quarantined as `non_vessel_mmsi`; the `vessels` table has a CHECK on the ship-station range |
| Position "not available" (lat 91, lon 181) | 1.2% | Quarantined as `position_unavailable` rather than stored at a fake location |
| No UTC second in the report (second > 59) | 2.0% | The idempotency key falls back to the receive second for these; two providers may then disagree by a second (documented limitation) |
| Heading 360-510, course 360+ (illegal, not the sentinel) | 0.04% | Nulled in the decoder: a broken ancillary field does not cost the position it arrived with |
| Static reports carrying no IMO number | 44% of type 5 | `imo` is nullable; where present its check digit is verified, and failures are stored as NULL, never as a wrong identity |
| Type 5 payloads shorter than the standard's 424 bits | present | Accepted from 420 bits and zero-padded |
| Malformed or checksum-failing sentences | 0.01% | Quarantined with the raw line; small here, but the simulator injects them at 0.8% to keep the path honest |

The simulator (`ghostfleet/simulate.py`) reproduces these rates, so the system is developed against data
with the same shape and the same defects as the real feed.

---

## Full profile of `nmea-sample`

- sentences: 85,194
- byte-identical repeated sentences: 3,331 (3.91%)
- decoded position reports: 73,596 from 10,415 distinct MMSIs
- decoded static reports: 2,528
- speed over ground, knots: p50 0.1, p90 13.1, p99 23.7, max 102.2
- stationary (sog < 0.5 kn): 42,250 (58.35%)

### Framing

| value | count |
|---|---|
| single_part | 80,342 (94.30%) |
| multipart_fragment | 4,844 (5.69%) |
| malformed_sentence | 8 (0.01%) |
| decode:truncated_payload | 6 (0.01%) |
| decode:unknown_message_type | 2 (0.00%) |
| dropped:orphan_fragment | 2 (0.00%) |
| dropped:incomplete_multipart | 2 (0.00%) |

### Message types (complete messages)

| value | count |
|---|---|
| type 1 | 61,368 (74.17%) |
| type 3 | 8,785 (10.62%) |
| other (unused types) | 6,618 (8.00%) |
| type 5 | 2,221 (2.68%) |
| type 2 | 1,973 (2.38%) |
| type 18 | 1,361 (1.64%) |
| type 24 | 307 (0.37%) |
| type 19 | 109 (0.13%) |

### MMSI class of position reports

| value | count |
|---|---|
| ship (MID 2xx-7xx) | 72,450 (98.44%) |
| other (00x) | 450 (0.61%) |
| other (99x) | 153 (0.21%) |
| other (02x) | 132 (0.18%) |
| other (12x) | 77 (0.10%) |
| other (01x) | 64 (0.09%) |
| other (03x) | 50 (0.07%) |
| other (90x) | 47 (0.06%) |
| other (04x) | 25 (0.03%) |
| other (09x) | 20 (0.03%) |
| other (11x) | 20 (0.03%) |
| other (10x) | 18 (0.02%) |
| other (96x) | 14 (0.02%) |
| other (08x) | 14 (0.02%) |
| other (80x) | 14 (0.02%) |
| other (06x) | 13 (0.02%) |
| other (84x) | 10 (0.01%) |
| other (88x) | 8 (0.01%) |
| other (92x) | 6 (0.01%) |
| other (86x) | 3 (0.00%) |
| other (81x) | 3 (0.00%) |
| other (85x) | 2 (0.00%) |
| other (14x) | 1 (0.00%) |
| other (16x) | 1 (0.00%) |
| other (13x) | 1 (0.00%) |

### Fields carrying the 'not available' sentinel

| value | count |
|---|---|
| position.heading | 32,819 (44.59%) |
| position.second (>59: no UTC second) | 1,456 (1.98%) |
| position.cog | 1,354 (1.84%) |
| position.sog | 1,193 (1.62%) |
| static.imo | 971 (1.32%) |
| static.destination | 413 (0.56%) |
| static.callsign | 183 (0.25%) |
| static.name | 12 (0.02%) |

### Position reports failing validation (quarantined)

| value | count |
|---|---|
| non_vessel_mmsi | 1,146 (1.56%) |
| position_unavailable | 911 (1.24%) |
| position_out_of_range | 7 (0.01%) |
