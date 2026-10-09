// @vitest-environment node
import fc from "fast-check";
import { chooseEvictions, chooseTiles, exposure, pointsInView, tileKey } from "./lod";
import type { TileRef, Variant } from "./types";

/** A random quadtree: every tile's parent exists, as the pipeline guarantees. */
const treeArb = fc.array(fc.tuple(fc.integer({ min: 0, max: 3 }), fc.integer({ min: 1, max: 1000 })), { minLength: 0, maxLength: 60 }).map((steps) => {
  const tiles: TileRef[] = [[0, 0, 0, 1000]];
  const seen = new Set(["0-0-0"]);
  for (const [quadrant, n] of steps) {
    const parent = tiles[(n * 7919) % tiles.length];
    const t: TileRef = [parent[0] + 1, parent[1] * 2 + (quadrant & 1), parent[2] * 2 + (quadrant >> 1), n];
    if (parent[0] < 6 && !seen.has(tileKey(t))) {
      seen.add(tileKey(t));
      tiles.push(t);
    }
  }
  return { bounds: [0, 0, 64], tiles } as unknown as Variant;
});
const viewArb = fc.tuple(fc.double({ min: -10, max: 70, noNaN: true }), fc.double({ min: -10, max: 70, noNaN: true }), fc.double({ min: 0.01, max: 80, noNaN: true })).map(([x, y, s]) => ({ x0: x, y0: y, x1: x + s, y1: y + s }));

test("the tiles chosen for a view never exceed the point budget, and are whole levels, coarse first", () => {
  fc.assert(
    fc.property(treeArb, viewArb, fc.integer({ min: 1, max: 20_000 }), (variant, view, budget) => {
      const chosen = chooseTiles(variant, view, budget);
      const total = chosen.reduce((s, t) => s + t[3], 0);
      const visible = variant.tiles.filter((t) => {
        const side = 64 / 2 ** t[0];
        return !(t[1] * side > view.x1 || (t[1] + 1) * side < view.x0 || t[2] * side > view.y1 || (t[2] + 1) * side < view.y0);
      });
      const roots = visible.filter((t) => t[0] === 0).reduce((s, t) => s + t[3], 0);
      expect(total).toBeLessThanOrEqual(Math.max(budget, roots)); // the root alone may exceed a tiny budget
      expect(chosen.map((t) => t[0])).toEqual([...chosen.map((t) => t[0])].sort((a, b) => a - b));
      const depth = Math.max(-1, ...chosen.map((t) => t[0]));
      // every visible tile down to the chosen depth is there: no holes inside a level
      expect(new Set(chosen.map(tileKey))).toEqual(new Set(visible.filter((t) => t[0] <= depth).map(tileKey)));
      // and a bigger budget never shows less
      expect(chooseTiles(variant, view, budget * 2).length).toBeGreaterThanOrEqual(chosen.length);
    }),
    { numRuns: 1000, seed: 15 },
  );
});

test("eviction keeps memory under the cap without ever dropping a tile the view wants", () => {
  fc.assert(
    fc.property(fc.array(fc.tuple(fc.integer({ min: 1, max: 500 }), fc.integer({ min: 0, max: 1000 }), fc.boolean()), { maxLength: 40 }), fc.integer({ min: 0, max: 5000 }), (entries, cap) => {
      const loaded = new Map(entries.map(([n, lastWanted], i) => [`k${i}`, { n, lastWanted }]));
      const wanted = new Set(entries.flatMap(([, , w], i) => (w ? [`k${i}`] : [])));
      const drop = chooseEvictions(loaded, wanted, cap);
      expect(drop.some((k) => wanted.has(k))).toBe(false);
      const kept = [...loaded].filter(([k]) => !drop.includes(k));
      const total = kept.reduce((s, [, t]) => s + t.n, 0);
      const wantedTotal = kept.filter(([k]) => wanted.has(k)).reduce((s, [, t]) => s + t.n, 0);
      expect(total <= cap || total === wantedTotal).toBe(true); // over the cap only if everything left is wanted
      // least recently wanted goes first
      const droppedAges = drop.map((k) => loaded.get(k)!.lastWanted);
      const keptIdle = kept.filter(([k]) => !wanted.has(k)).map(([, t]) => t.lastWanted);
      if (droppedAges.length && keptIdle.length) expect(Math.max(...droppedAges)).toBeLessThanOrEqual(Math.min(...keptIdle));
    }),
    { numRuns: 1000, seed: 15 },
  );
});

test("roaming with a budget holds a bounded number of points however large the dataset", () => {
  // A full quadtree six levels deep: 5,461 tiles of 1,000 points = 5.5 million points.
  const tiles: TileRef[] = [];
  for (let z = 0; z <= 6; z++) for (let x = 0; x < 2 ** z; x++) for (let y = 0; y < 2 ** z; y++) tiles.push([z, x, y, 1000]);
  const variant = { bounds: [0, 0, 64], tiles } as unknown as Variant;
  const budget = 50_000, cap = budget * 1.25;
  const loaded = new Map<string, { n: number; lastWanted: number }>();
  let peak = 0, s = 1;
  const rnd = (): number => (s = (s * 1103515245 + 12345) % 2 ** 31) / 2 ** 31;
  for (let step = 0; step < 400; step++) {
    const size = 64 / 2 ** Math.floor(rnd() * 7), x = rnd() * (64 - size), y = rnd() * (64 - size);
    const wanted = chooseTiles(variant, { x0: x, y0: y, x1: x + size, y1: y + size }, budget);
    for (const t of wanted) loaded.set(tileKey(t), { n: t[3], lastWanted: step });
    for (const key of chooseEvictions(loaded, new Set(wanted.map(tileKey)), cap)) loaded.delete(key);
    peak = Math.max(peak, [...loaded.values()].reduce((sum, t) => sum + t.n, 0));
  }
  expect(peak).toBeLessThanOrEqual(cap);
  expect(peak).toBeGreaterThan(budget / 2); // and the budget is actually used
});

test("exposure follows the densest visible cell and the share of points drawn", () => {
  const grid = new Uint32Array(16);
  grid[5] = 400; // cell (1, 1)
  grid[15] = 10_000; // cell (3, 3)
  const bounds = [0, 0, 4] as const;
  const near = { x0: 0, y0: 0, x1: 2, y1: 2 }, all = { x0: 0, y0: 0, x1: 4, y1: 4 };
  expect(pointsInView(grid, 4, bounds, near)).toBe(400);
  expect(pointsInView(grid, 4, bounds, all)).toBe(10_400);
  const a = exposure(grid, 4, bounds, near, 10, 2, 1), b = exposure(grid, 4, bounds, all, 10, 2, 1);
  expect(b / a).toBeCloseTo(25);
  expect(exposure(grid, 4, bounds, all, 10, 2, 0.5)).toBeCloseTo(b / 2);
  expect(exposure(new Uint32Array(16), 4, bounds, all, 10, 2, 1)).toBe(1.5); // an empty view still has a sane scale
});
