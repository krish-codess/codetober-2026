// The spatial index the worker holds: hit testing and lasso selection over the loaded tiles.
//
// Two levels of quadtree. The tiles themselves are the upper levels (a tile is a square of the
// plane). Inside a tile the points are sorted by Morton code, so every quadtree cell of the tile
// is one contiguous run of the arrays, found by binary search: a linear quadtree with no nodes to
// build or store. A lasso descends it and only tests individual points in the cells its outline
// actually crosses; cells wholly inside are accepted as a run.

import { INSIDE, OUTSIDE, bboxOf, classifyRect, pointInPolygon } from "./geometry";
import { Q, type Tile } from "./tile";

export const COLUMNS = 3;
const LEAF = 24; // below this many points, testing each is cheaper than descending

export interface Selection {
  n: number;
  /** per column, points selected per category code */
  counts: Uint32Array[];
  /** per tile key, 255 where the point is selected: uploaded to the GPU as-is */
  flags: Map<string, Uint8Array>;
  ids: Uint32Array | null;
}

export interface Hit {
  id: number;
  x: number;
  y: number;
  cats: [number, number, number];
}

/** allow[column * 256 + code] is 1 when that category passes the filter */
export type Allow = Uint8Array;

function lowerBound(a: Uint32Array, value: number, lo: number, hi: number): number {
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (a[mid] < value) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * Visit the points of `tile` that can lie in a region. `classify` places a cell's rectangle
 * relative to the region; `accept(i)` is called for every point of an INSIDE cell and for the
 * points of crossing cells that pass `test(i)`.
 */
function walk(tile: Tile, classify: (x0: number, y0: number, x1: number, y1: number) => number, test: (i: number) => boolean, accept: (i: number) => void): void {
  const step = tile.side / Q;
  // Stored coordinates are float32: widen each cell by more than that rounding can move a point.
  const pad = (Math.abs(tile.x) + Math.abs(tile.y) + tile.side) * 2.5e-7;
  const { codes } = tile;
  const visit = (lo: number, hi: number, level: number, cx: number, cy: number, base: number): void => {
    if (lo >= hi) return;
    const cell = 2 ** (16 - level);
    const where = classify(tile.x + cx * cell * step - pad, tile.y + cy * cell * step - pad, tile.x + ((cx + 1) * cell - 1) * step + pad, tile.y + ((cy + 1) * cell - 1) * step + pad);
    if (where === OUTSIDE) return;
    if (where === INSIDE) {
      for (let i = lo; i < hi; i++) accept(i);
    } else if (hi - lo <= LEAF || level === 16) {
      for (let i = lo; i < hi; i++) if (test(i)) accept(i);
    } else {
      const span = 4 ** (15 - level); // codes covered by one child
      let from = lo;
      for (let c = 0; c < 4; c++) {
        const to = c === 3 ? hi : lowerBound(codes, base + (c + 1) * span, from, hi);
        visit(from, to, level + 1, cx * 2 + (c & 1), cy * 2 + (c >> 1), base + c * span);
        from = to;
      }
    }
  };
  visit(0, tile.n, 0, 0, 0, 0);
}

export class PointIndex {
  readonly tiles = new Map<string, Tile>();

  add(tile: Tile): void {
    this.tiles.set(tile.key, tile);
  }

  remove(key: string): void {
    this.tiles.delete(key);
  }

  clear(): void {
    this.tiles.clear();
  }

  get size(): number {
    let n = 0;
    for (const t of this.tiles.values()) n += t.n;
    return n;
  }

  /** Points inside the polygon (even-odd) that pass the category filter. */
  select(poly: ArrayLike<number>, allow: Allow | null, wantIds: boolean): Selection {
    const counts = Array.from({ length: COLUMNS }, () => new Uint32Array(256));
    const flags = new Map<string, Uint8Array>();
    const ids = wantIds ? new Uint32Array(this.size) : null;
    const [bx0, by0, bx1, by1] = bboxOf(poly);
    let n = 0;
    for (const tile of this.tiles.values()) {
      const f = new Uint8Array(tile.n);
      flags.set(tile.key, f);
      if (poly.length < 6 || bx1 < tile.x || bx0 > tile.x + tile.side || by1 < tile.y || by0 > tile.y + tile.side) continue;
      const { cats, wx, wy } = tile;
      walk(
        tile,
        (x0, y0, x1, y1) => (x1 < bx0 || x0 > bx1 || y1 < by0 || y0 > by1 ? OUTSIDE : classifyRect(poly, x0, y0, x1, y1)),
        (i) => pointInPolygon(wx[i], wy[i], poly),
        (i) => {
          const c0 = cats[4 * i], c1 = cats[4 * i + 1], c2 = cats[4 * i + 2];
          if (allow && !(allow[c0] && allow[256 + c1] && allow[512 + c2])) return;
          f[i] = 255;
          counts[0][c0]++;
          counts[1][c1]++;
          counts[2][c2]++;
          if (ids) ids[n] = tile.ids[i];
          n++;
        },
      );
    }
    return { n, counts, flags, ids: ids ? ids.subarray(0, n) : null };
  }

  /** The nearest point within `r` of (x, y) that passes the filter, or null. */
  hit(x: number, y: number, r: number, allow: Allow | null): Hit | null {
    let best: Hit | null = null;
    let bestD = r * r;
    const qx0 = x - r, qy0 = y - r, qx1 = x + r, qy1 = y + r;
    for (const tile of this.tiles.values()) {
      if (qx1 < tile.x || qx0 > tile.x + tile.side || qy1 < tile.y || qy0 > tile.y + tile.side) continue;
      const { cats, wx, wy } = tile;
      walk(
        tile,
        (x0, y0, x1, y1) => (x1 < qx0 || x0 > qx1 || y1 < qy0 || y0 > qy1 ? OUTSIDE : 2),
        () => true,
        (i) => {
          const d = (wx[i] - x) ** 2 + (wy[i] - y) ** 2;
          if (d > bestD) return;
          const c: [number, number, number] = [cats[4 * i], cats[4 * i + 1], cats[4 * i + 2]];
          if (allow && !(allow[c[0]] && allow[256 + c[1]] && allow[512 + c[2]])) return;
          bestD = d;
          best = { id: tile.ids[i], x: wx[i], y: wy[i], cats: c };
        },
      );
    }
    return best;
  }

  /** Positions of the given row ids among the loaded tiles: [id, x, y] triples for those found. */
  find(ids: ArrayLike<number>): { id: number; x: number; y: number }[] {
    const want = new Set<number>(Array.from(ids));
    const out: { id: number; x: number; y: number }[] = [];
    if (!want.size) return out;
    for (const tile of this.tiles.values()) for (let i = 0; i < tile.n; i++) if (want.has(tile.ids[i])) out.push({ id: tile.ids[i], x: tile.wx[i], y: tile.wy[i] });
    return out;
  }
}
