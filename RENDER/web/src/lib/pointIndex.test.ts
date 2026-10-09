// @vitest-environment node
// The selection guarantee: the quadtree returns exactly the points a brute-force scan would.
import fc from "fast-check";
import { INSIDE, OUTSIDE, classifyRect, pointInPolygon, simplify } from "./geometry";
import { PointIndex, type Allow } from "./pointIndex";
import { encodeTile, type RawPoint } from "./testing";
import { decodeTile, morton, tileSquare, TileError } from "./tile";

const BOUNDS: [number, number, number] = [-8, -8, 16];

// Coordinates from a coarse lattice as often as not: points on top of each other, on cell
// boundaries and exactly on polygon edges are the cases a spatial index gets wrong.
const coord = fc.oneof(fc.double({ min: -8, max: 7.99, noNaN: true }), fc.integer({ min: -8, max: 7 }), fc.integer({ min: -16, max: 15 }).map((v) => v / 2));
const pointsArb = fc.array(fc.tuple(coord, coord, fc.integer({ min: 0, max: 3 }), fc.integer({ min: 0, max: 2 })), { minLength: 0, maxLength: 400 });
const polyArb = fc.array(fc.tuple(coord, coord), { minLength: 3, maxLength: 12 }).map((p) => p.flat());

/** Split points over a root tile and its four children, as the pipeline would for a small capacity. */
function indexOf(pts: [number, number, number, number][]): { index: PointIndex; all: { id: number; x: number; y: number; cats: number[] }[] } {
  const index = new PointIndex();
  const groups = new Map<string, RawPoint[]>();
  pts.forEach(([x, y, c0, c1], id) => {
    const deep = id % 3 !== 0;
    const tx = deep ? (x >= 0 ? 1 : 0) : 0, ty = deep ? (y >= 0 ? 1 : 0) : 0;
    const key = `${deep ? 1 : 0}-${tx}-${ty}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push({ id, x, y, cats: [c0, c1, 1] });
  });
  const all: { id: number; x: number; y: number; cats: number[] }[] = [];
  for (const [key, group] of groups) {
    const [z, x, y] = key.split("-").map(Number);
    const sq = tileSquare(BOUNDS, z, x, y);
    const tile = decodeTile(encodeTile(group, sq), key, z, group.length, sq);
    index.add(tile);
    for (let i = 0; i < tile.n; i++) all.push({ id: tile.ids[i], x: tile.wx[i], y: tile.wy[i], cats: [tile.cats[4 * i], tile.cats[4 * i + 1], tile.cats[4 * i + 2]] });
  }
  return { index, all };
}

const sorted = (ids: Iterable<number>): number[] => [...ids].sort((a, b) => a - b);

test("lasso selection equals a brute-force scan, for any points and any polygon", () => {
  fc.assert(
    fc.property(pointsArb, polyArb, fc.boolean(), (pts, poly, filtered) => {
      const { index, all } = indexOf(pts);
      let allow: Allow | null = null;
      if (filtered) {
        allow = new Uint8Array(768).fill(1);
        allow[2] = 0; // hide script code 2
        allow[256 + 0] = 0; // and kind code 0
      }
      const expected = all.filter((p) => pointInPolygon(p.x, p.y, poly) && (!allow || (allow[p.cats[0]] && allow[256 + p.cats[1]])));
      const got = index.select(poly, allow, true);
      expect(sorted(got.ids!)).toEqual(sorted(expected.map((p) => p.id)));
      expect(got.n).toBe(expected.length);
      // The three outputs describe the same set: ids, per-category counts, and the GPU flags.
      for (const col of [0, 1, 2]) expect(got.counts[col].reduce((a, b) => a + b, 0)).toBe(got.n);
      for (let code = 0; code < 4; code++) expect(got.counts[0][code]).toBe(expected.filter((p) => p.cats[0] === code).length);
      let flagged = 0;
      for (const [key, flags] of got.flags) {
        expect(flags.length).toBe(index.tiles.get(key)!.n);
        for (const f of flags) flagged += f === 255 ? 1 : 0;
      }
      expect(flagged).toBe(got.n);
      expect(index.select(poly, allow, false).ids).toBeNull();
    }),
    { numRuns: 1000, seed: 15 },
  );
});

test("a dense tile takes the fast path through whole cells and still agrees", () => {
  // 20,000 points and big polygons: most cells are wholly inside, so this exercises range acceptance.
  let s = 42;
  const rnd = (): number => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32) * 16 - 8;
  const pts = Array.from({ length: 20_000 }, (_, i): [number, number, number, number] => [i % 7 ? rnd() : Math.round(rnd()), i % 5 ? rnd() : Math.round(rnd()), i % 4, i % 3]);
  const { index, all } = indexOf(pts);
  fc.assert(
    fc.property(polyArb, (poly) => {
      const expected = all.filter((p) => pointInPolygon(p.x, p.y, poly)).map((p) => p.id);
      expect(sorted(index.select(poly, null, true).ids!)).toEqual(sorted(expected));
    }),
    { numRuns: 60, seed: 15 },
  );
});

test("classifyRect never calls a rectangle inside or outside when a point in it disagrees", () => {
  fc.assert(
    fc.property(polyArb, coord, coord, fc.double({ min: 0, max: 4, noNaN: true }), fc.double({ min: 0, max: 4, noNaN: true }), fc.array(fc.tuple(fc.double({ min: 0, max: 1, noNaN: true }), fc.double({ min: 0, max: 1, noNaN: true })), { maxLength: 30 }), (poly, x0, y0, w, h, samples) => {
      const verdict = classifyRect(poly, x0, y0, x0 + w, y0 + h);
      if (verdict !== INSIDE && verdict !== OUTSIDE) return;
      const probes: [number, number][] = [[0, 0], [1, 0], [0, 1], [1, 1], [0.5, 0.5], ...samples];
      for (const [u, v] of probes) expect(pointInPolygon(x0 + u * w, y0 + v * h, poly)).toBe(verdict === INSIDE);
    }),
    { numRuns: 3000, seed: 15 },
  );
});

test("hover finds the nearest point within the radius, like a linear scan", () => {
  fc.assert(
    fc.property(pointsArb, coord, coord, fc.double({ min: 0.01, max: 3, noNaN: true }), (pts, x, y, r) => {
      const { index, all } = indexOf(pts);
      let best = Infinity;
      for (const p of all) best = Math.min(best, (p.x - x) ** 2 + (p.y - y) ** 2);
      const hit = index.hit(x, y, r, null);
      if (best > r * r) expect(hit).toBeNull();
      else expect((hit!.x - x) ** 2 + (hit!.y - y) ** 2).toBe(best);
    }),
    { numRuns: 500, seed: 15 },
  );
});

test("find, remove and clear", () => {
  const { index, all } = indexOf([[1, 1, 0, 0], [-1, -1, 0, 0], [2, 2, 0, 0], [-3, 3, 0, 0]]);
  expect(index.size).toBe(4);
  expect(index.find([2, 99]).map((p) => p.id)).toEqual([2]);
  expect(index.find([])).toEqual([]);
  const everything = [-9, -9, 9, -9, 9, 9, -9, 9];
  expect(index.select(everything, null, false).n).toBe(all.length);
  index.remove("1-1-1");
  expect(index.select(everything, null, false).n).toBeLessThan(all.length);
  index.clear();
  expect(index.size).toBe(0);
  expect(index.select(everything, null, true).ids).toEqual(new Uint32Array(0));
});

test("a tile that is not what the manifest promised is rejected", () => {
  const sq = tileSquare(BOUNDS, 0, 0, 0);
  const good = encodeTile([{ id: 1, x: 0, y: 0 }, { id: 2, x: 3, y: 3 }, { id: 3, x: -5, y: 2 }], sq);
  expect(decodeTile(good, "t", 0, 3, sq).n).toBe(3);
  expect(() => decodeTile(good, "t", 0, 4, sq)).toThrow(/manifest says 4/);
  expect(() => decodeTile(good.slice(0, 30), "t", 0, 3, sq)).toThrow(TileError);
  expect(() => decodeTile(good.slice(0, 7), "t", 0, 3, sq)).toThrow(/truncated/);
  expect(() => decodeTile(new TextEncoder().encode("<html>502 Bad Gateway</html>").buffer as ArrayBuffer, "t", 0, 3, sq)).toThrow(/not a format-1 tile/);
  const shuffled = good.slice(0);
  const q = new Uint16Array(shuffled, 16 + 12, 6);
  q.set([65535, 65535, 0, 0, 1, 1]);
  expect(() => decodeTile(shuffled, "t", 0, 3, sq)).toThrow(/Morton/);
});

test("morton codes order points by quadrant, then sub-quadrant", () => {
  expect(morton(0, 0)).toBe(0);
  expect(morton(65535, 65535)).toBe(0xffffffff);
  expect(morton(1, 0)).toBe(1);
  expect(morton(0, 1)).toBe(2);
  expect(morton(0x8000, 0)).toBeLessThan(morton(0, 0x8000));
});

test("simplify keeps a lasso small without moving its ends", () => {
  const path = Array.from({ length: 4000 }, (_, i) => [Math.cos(i / 600), Math.sin(i / 600)]).flat();
  const out = simplify(path, 0.01);
  expect(out.length).toBeLessThanOrEqual(1024);
  expect(out.slice(0, 2)).toEqual(path.slice(0, 2));
  expect(simplify(path, 0, 50).length).toBeLessThanOrEqual(100);
});
