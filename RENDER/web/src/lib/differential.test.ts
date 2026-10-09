// @vitest-environment node
// Differential test against the Python reference implementation (tests/test_web_fixture.py wrote
// the tiles and what dots/api.py selects in them). Same bytes in, same row ids out.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { PointIndex, type Allow } from "./pointIndex";
import { summarise } from "./stats";
import { decodeTile, tileSquare } from "./tile";
import type { Column } from "./types";

const dir = join(__dirname, "__fixtures__");
const fixture = JSON.parse(readFileSync(join(dir, "fixture.json"), "utf8")) as {
  n: number;
  bounds: [number, number, number];
  tiles: [number, number, number, number][];
  columns: Column[];
  cases: { polygon: [number, number][]; allow: Record<string, number[]> | null; ids: number[]; summary: { name: string; purity: number | null; entropy: number | null; counts: { label: string; count: number; share: number; lift: number | null }[] }[] }[];
};

const index = new PointIndex();
for (const [z, x, y, n] of fixture.tiles) {
  const bytes = readFileSync(join(dir, `${z}-${x}-${y}.bin`));
  index.add(decodeTile(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer, `${z}-${x}-${y}`, z, n, tileSquare(fixture.bounds, z, x, y)));
}

test("the fixture loaded whole", () => {
  expect(index.size).toBe(fixture.n);
  expect(fixture.cases.length).toBeGreaterThan(50);
});

test.each(fixture.cases.map((c, i) => [i, c] as const))("case %i selects exactly the rows the Python reference selects", (_, c) => {
  let allow: Allow | null = null;
  if (c.allow) {
    allow = new Uint8Array(768);
    fixture.columns.forEach((col, ci) => c.allow![col.name].forEach((code) => (allow![ci * 256 + code] = 1)));
  }
  const got = index.select(c.polygon.flat(), allow, true);
  expect([...got.ids!].sort((a, b) => a - b)).toEqual(c.ids);

  const summary = summarise(fixture.columns, got.counts, fixture.n);
  summary.forEach((col, ci) => {
    const ref = c.summary[ci];
    expect(col.counts.map((x) => [x.label, x.count])).toEqual(ref.counts.map((x) => [x.label, x.count]));
    if (ref.purity === null) expect(col.purity).toBeNull();
    else expect(col.purity).toBeCloseTo(ref.purity, 12);
    if (ref.entropy === null) expect(col.entropy).toBeNull();
    else expect(col.entropy).toBeCloseTo(ref.entropy, 12);
    col.counts.forEach((x, k) => {
      expect(x.share).toBeCloseTo(ref.counts[k].share, 12);
      expect(x.lift).toBeCloseTo(ref.counts[k].lift!, 10);
    });
  });
});
