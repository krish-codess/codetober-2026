import { routeOf } from "./App";
import type { LiveRow, TrackPoint } from "./api";
import { ago, duration, Fleet, MAX_EXTRAPOLATE_S, positionAt, segments, utc } from "./fleet";
import { bboxOf } from "./useDeck";

describe("Fleet", () => {
  it("dead-reckons a moving vessel and leaves a stationary one alone", () => {
    const fleet = new Fleet();
    fleet.upsert([
      [257000001, 5, 0, 10, 90, null, 1000], // 10 kn due east on the equator
      [257000002, 5, 60, 0.2, 90, null, 1000],
      [257000003, 5, 60, 12, null, null, 1000], // no course: we do not guess a direction
    ]);
    const out = fleet.positionsAt(1060, new Float32Array(6));
    expect(out[0]).toBeCloseTo(5 + (10 * 0.514444 * 60) / 111_320, 5);
    expect(out[1]).toBeCloseTo(0, 6);
    expect([out[2], out[3], out[4], out[5]]).toEqual([5, 60, 5, 60]);
  });

  it("stops extrapolating once the fix is too old to trust", () => {
    const fleet = new Fleet();
    fleet.upsert([[257000001, 5, 0, 20, 0, null, 1000]]);
    const atCap = fleet.positionsAt(1000 + MAX_EXTRAPOLATE_S, new Float32Array(2))[1];
    expect(atCap).toBeGreaterThan(0);
    expect(fleet.positionsAt(1000 + 3600, new Float32Array(2))[1]).toBe(atCap);
    expect(fleet.positionsAt(900, new Float32Array(2))[1]).toBe(0); // never backwards before the fix
  });

  it("updates in place, ignores late arrivals, and grows past its first allocation", () => {
    const fleet = new Fleet();
    fleet.upsert([[257000001, 5, 60, 0, null, null, 1000]]);
    fleet.upsert([[257000001, 6, 60, 0, null, null, 2000]]);
    fleet.upsert([[257000001, 4, 60, 0, null, null, 1500]]); // older than what we hold
    expect(fleet.size).toBe(1);
    expect(fleet.positionsAt(2000, new Float32Array(2))[0]).toBe(6);
    fleet.upsert(Array.from({ length: 3000 }, (_, i): LiveRow => [300000000 + i, i / 100, 10, 0, null, null, 1000]));
    expect(fleet.size).toBe(3001);
    expect(fleet.mmsi[3000]).toBe(300002999);
    expect(fleet.positionsAt(1000, new Float32Array(2 * fleet.mmsi.length))[2 * 3000]).toBeCloseTo(29.99, 2);
  });
});

describe("track helpers", () => {
  const track: TrackPoint[] = [
    [0, 0, 0, 5, 0],
    [100, 1, 2, 5, 0],
    [5000, 3, 2, 5, 0],
    [5100, 4, 2, 5, 0],
  ];
  it("interpolates between fixes and clamps at the ends", () => {
    expect(positionAt(track, 50)).toEqual([0.5, 1]);
    expect(positionAt(track, -10)).toEqual([0, 0]);
    expect(positionAt(track, 9999)).toEqual([4, 2]);
    expect(positionAt([], 5)).toBeNull();
  });
  it("breaks the drawn track where the vessel was silent", () => {
    expect(segments(track, 1800)).toEqual([
      [
        [0, 0],
        [1, 2],
      ],
      [
        [3, 2],
        [4, 2],
      ],
    ]);
    expect(segments(track, 10_000)).toHaveLength(1);
    expect(segments([], 1800)).toEqual([]);
  });
});

describe("formatting and routing", () => {
  it("formats durations, times and ages", () => {
    expect([duration(59), duration(2760), duration(3600), duration(90000)]).toEqual(["1 min", "46 min", "1 h 00 min", "1 d 1 h"]);
    expect(utc("2026-10-09T17:59:59Z")).toBe("2026-10-09 17:59 UTC");
    expect(utc(1791568800)).toBe("2026-10-09 18:00 UTC");
    expect([ago(12), ago(600)]).toEqual(["12 s ago", "10 min ago"]);
  });
  it("accepts only a nine-digit MMSI in the route", () => {
    expect(routeOf("#/vessel/257000001")).toBe(257000001);
    for (const bad of ["", "#/", "#/vessel/12", "#/vessel/257000001/x", "#/vessel/abc"]) expect(routeOf(bad)).toBeNull();
  });
  it("turns a view into a valid API bbox, even zoomed all the way out or at the edge", () => {
    const views = [
      { longitude: 0, latitude: 0, zoom: 0.5 },
      { longitude: 179, latitude: 80, zoom: 3 },
      { longitude: 5, latitude: 60, zoom: 9 },
    ];
    for (const view of views) {
      const [w, s, e, n] = bboxOf({ ...view, width: 1200, height: 800 }).split(",").map(Number);
      expect(w).toBeGreaterThanOrEqual(-180);
      expect(e).toBeLessThanOrEqual(180);
      expect(n).toBeLessThanOrEqual(90);
      expect(w).toBeLessThan(e);
      expect(s).toBeLessThan(n);
    }
  });
});
