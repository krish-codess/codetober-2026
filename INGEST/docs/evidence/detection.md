# Evidence: coverage gaps versus genuine dark periods

**Claim.** The detector tells a vessel that switched off its transponder apart from one that sailed out of
receiver range or sat under a receiver that was down, and it flags reappearances that would have required
an impossible speed.

**How it is tested.** Ground truth for this does not exist in real AIS data: nobody publishes which
silences were deliberate. So the simulator generates a world where the cause of every silence is known:

- vessels sail out past the receivers' 85 km range and back (cause: `range`)
- one receiver in every third port has a recurring 50-minute outage (cause: `outage`)
- 6% of moving vessels switch off inside coverage for 45 to 120 minutes (cause: `dark`)
- 2% switch off and reappear at a different port thousands of miles away (`dark` + teleport)

The detector sees only the resulting NMEA stream, defects included, through the normal pipeline
(`replay` -> Kafka -> sink -> PostGIS -> `detect`). `ghostfleet evaluate` then compares its verdicts with the truth.

## Result (1,500 ships, 12 hours, 454,483 stored positions, 324 true silences of moving vessels)

At the default `GHOST_COVERAGE_MIN=0.8`:

| ground truth \ verdict | dark | coverage_gap | not detected |
|---|---|---|---|
| transponder off | 120 | 10 | 0 |
| out of receiver range | 14 | 164 | 0 |
| receiver outage | 4 | 12 | 0 |

- precision of `dark`: 120/138 = **0.870**
- recall of `dark`: 120/130 = **0.923**
- innocent silences correctly left as `coverage_gap`: 176/194
- impossible-speed reappearances flagged: **27/27**; flagged without cause: 0
- detector events with no ground-truth silence behind them: 0
- every true silence was detected as a gap of one class or the other

## The threshold is a dial, and this is what it trades

| `GHOST_COVERAGE_MIN` | precision | recall | false alarms from range | false alarms from outages |
|---|---|---|---|---|
| 0.50 | 0.734 | 0.954 | 34 | 11 |
| **0.80** (default) | **0.870** | **0.923** | 14 | 4 |
| 0.95 | 0.957 | 0.846 | 1 | 4 |

The default leans towards precision: the ranking exists to spend an investigator's attention well.

## Where it is wrong, and why

- **14 range gaps called dark.** Vessels that turned around just beyond the coverage edge. Dead reckoning
  projects 30 minutes ahead on the last course; a vessel that leaves coverage more slowly than that, in a
  cell that other traffic keeps "reliable", looks covered.
- **4 outage gaps called dark.** Partial outages. Each simulated port has three overlapping receivers; when
  one fails, a neighbour 15 km away can still be heard by another receiver, which the detector reads as
  proof that reception was fine. Receiver identity is not available in the feed, so this cannot be resolved
  from the data alone.
- **10 dark periods called coverage gaps.** Vessels that went dark near the edge of coverage. Conservative
  by design: the system does not accuse where it cannot show reception.

## What this does not prove

The numbers describe the simulator's world: clean circular coverage, dense traffic near ports, and dark
behaviour that is unsubtle. Real coverage is ragged and changes with weather; real evasion includes
spoofed positions and swapped identities, which this detector does not model. The result shows that the
mechanism separates the three causes when they are separable from positions alone; it is not a field
accuracy figure. The integration test `test_detector_separates_dark_from_coverage_and_is_idempotent`
keeps a floor of 0.8 on both precision and recall for a smaller world, so a regression fails CI.

Reproduce:

```
python -m ghostfleet sim --ships 1500 --hours 12 --out data/seed/sim/history.nmea --truth data/seed/truth.json
python -m ghostfleet replay data/seed/sim --source sim      # with `ghostfleet sink` running
python -m ghostfleet detect --once
python -m ghostfleet evaluate data/seed/truth.json
```
