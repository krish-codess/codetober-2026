// Level of detail: which tiles to hold for a view, and which to let go. Pure functions.

import type { TileRef, Variant } from "./types";

export interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export const tileKey = (t: TileRef): string => `${t[0]}-${t[1]}-${t[2]}`;

/**
 * Tiles to draw for a view, coarse levels first, never more than `budget` points.
 *
 * Whole levels only: depth d is added only if every visible tile of depth d fits. Each depth adds
 * an even sample of the view, so stopping between depths keeps the picture uniform, where
 * stopping in the middle of one would leave some tiles visibly denser than their neighbours.
 * The root level is always included so there is always something to draw.
 */
export function chooseTiles(variant: Variant, view: Rect, budget: number): TileRef[] {
  const [bx, by, size] = variant.bounds;
  const byDepth: TileRef[][] = [];
  for (const t of variant.tiles) {
    const side = size / 2 ** t[0];
    const x = bx + t[1] * side, y = by + t[2] * side;
    if (x > view.x1 || x + side < view.x0 || y > view.y1 || y + side < view.y0) continue;
    (byDepth[t[0]] ??= []).push(t);
  }
  const out: TileRef[] = [];
  let points = 0;
  for (let z = 0; z < byDepth.length; z++) {
    const level = byDepth[z] ?? [];
    const n = level.reduce((sum, t) => sum + t[3], 0);
    if (z > 0 && points + n > budget) break;
    out.push(...level);
    points += n;
  }
  return out;
}

/**
 * Tiles to drop so that at most `cap` points stay in memory: least recently wanted first, and
 * never one the current view wants. This is what keeps memory flat however far the user roams.
 */
export function chooseEvictions(loaded: Map<string, { n: number; lastWanted: number }>, wanted: Set<string>, cap: number): string[] {
  let total = 0;
  for (const t of loaded.values()) total += t.n;
  const out: string[] = [];
  const idle = [...loaded].filter(([key]) => !wanted.has(key)).sort((a, b) => a[1].lastWanted - b[1].lastWanted);
  for (const [key, t] of idle) {
    if (total <= cap) break;
    out.push(key);
    total -= t.n;
  }
  return out;
}

/**
 * The accumulated weight to treat as "densest" when tone-mapping a view, from the precomputed
 * density grid: the fullest visible grid cell, converted to points per screen pixel, times the
 * weight one point spreads over its sprite. `drawnShare` scales it when only a sample is drawn.
 */
export function exposure(grid: Uint32Array, cells: number, bounds: readonly [number, number, number], view: Rect, pxPerUnit: number, radiusPx: number, drawnShare: number): number {
  const cell = bounds[2] / cells;
  const i0 = Math.max(0, Math.floor((view.x0 - bounds[0]) / cell)), i1 = Math.min(cells - 1, Math.floor((view.x1 - bounds[0]) / cell));
  const j0 = Math.max(0, Math.floor((view.y0 - bounds[1]) / cell)), j1 = Math.min(cells - 1, Math.floor((view.y1 - bounds[1]) / cell));
  let peak = 0;
  for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) peak = Math.max(peak, grid[j * cells + i]);
  const perPixel = (peak * drawnShare) / (cell * pxPerUnit) ** 2;
  // Weight one point deposits: 1 for a one-pixel sprite, about 1.15 r^2 for a disc (the integral
  // of its exp(-2.5 d^2) falloff).
  return Math.max(1.5, perPixel * Math.max(1, 1.15 * radiusPx * radiusPx));
}

/** Points inside the view according to the density grid (for "showing n of m"). */
export function pointsInView(grid: Uint32Array, cells: number, bounds: readonly [number, number, number], view: Rect): number {
  const cell = bounds[2] / cells;
  const i0 = Math.max(0, Math.floor((view.x0 - bounds[0]) / cell)), i1 = Math.min(cells - 1, Math.floor((view.x1 - bounds[0]) / cell));
  const j0 = Math.max(0, Math.floor((view.y0 - bounds[1]) / cell)), j1 = Math.min(cells - 1, Math.floor((view.y1 - bounds[1]) / cell));
  let sum = 0;
  for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) sum += grid[j * cells + i];
  return sum;
}
