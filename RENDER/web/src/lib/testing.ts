// Test helper: encode points into a tile the way dots/tiles.py does.

import { CAT_BYTES, HEADER, Q, morton, type Square } from "./tile";

export interface RawPoint {
  id: number;
  x: number;
  y: number;
  cats?: [number, number, number];
}

export function encodeTile(points: RawPoint[], sq: Square): ArrayBuffer {
  const quantise = (v: number, origin: number): number => Math.min(Q, Math.max(0, Math.round(((v - origin) / sq.side) * Q)));
  const rows = points
    .map((p) => {
      const qx = quantise(p.x, sq.x), qy = quantise(p.y, sq.y);
      return { p, qx, qy, code: morton(qx, qy) };
    })
    .sort((a, b) => a.code - b.code);
  const n = rows.length;
  const buf = new ArrayBuffer(HEADER + n * (8 + CAT_BYTES));
  const view = new DataView(buf);
  view.setUint32(0, 0x53544f44, true);
  view.setUint16(4, 1, true);
  view.setUint16(6, CAT_BYTES, true);
  view.setUint32(8, n, true);
  rows.forEach((r, i) => {
    view.setUint32(HEADER + 4 * i, r.p.id, true);
    view.setUint16(HEADER + 4 * n + 4 * i, r.qx, true);
    view.setUint16(HEADER + 4 * n + 4 * i + 2, r.qy, true);
    (r.p.cats ?? [0, 0, 0]).forEach((c, k) => view.setUint8(HEADER + 8 * n + 4 * i + k, c));
  });
  return buf;
}
