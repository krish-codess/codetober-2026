// Binary tile format: see dots/tiles.py. 16-byte header, then ids u32[n], xy u16[2n], cats u8[4n].

export const HEADER = 16;
export const CAT_BYTES = 4;
export const Q = 65535;
const MAGIC = 0x53544f44; // "DOTS" read little-endian

export interface Square {
  x: number;
  y: number;
  side: number;
}

export interface Tile extends Square {
  key: string;
  z: number;
  n: number;
  ids: Uint32Array;
  /** quantised x, y interleaved: what the GPU draws */
  q: Uint16Array;
  cats: Uint8Array;
  /** float32 world coordinates, identical to dots.tiles.dequantise */
  wx: Float32Array;
  wy: Float32Array;
  /** Morton code of each point, ascending: the tile is a linear quadtree */
  codes: Uint32Array;
}

export class TileError extends Error {}

export function tileSquare(bounds: readonly [number, number, number], z: number, x: number, y: number): Square {
  const side = bounds[2] / 2 ** z;
  return { x: bounds[0] + x * side, y: bounds[1] + y * side, side };
}

function spread(v: number): number {
  v = (v | (v << 8)) & 0x00ff00ff;
  v = (v | (v << 4)) & 0x0f0f0f0f;
  v = (v | (v << 2)) & 0x33333333;
  return (v | (v << 1)) & 0x55555555;
}

/** x in the even bits, y in the odd bits, as an unsigned 32-bit number. */
export function morton(qx: number, qy: number): number {
  return (spread(qx) | (spread(qy) << 1)) >>> 0;
}

/** Validate and decode one tile. Throws TileError on anything that is not exactly what the manifest promised. */
export function decodeTile(buf: ArrayBuffer, key: string, z: number, expected: number, sq: Square): Tile {
  if (buf.byteLength < HEADER) throw new TileError(`tile ${key} is truncated (${buf.byteLength} bytes)`);
  const head = new DataView(buf);
  if (head.getUint32(0, true) !== MAGIC || head.getUint16(4, true) !== 1 || head.getUint16(6, true) !== CAT_BYTES)
    throw new TileError(`tile ${key} is not a format-1 tile`);
  const n = head.getUint32(8, true);
  if (n !== expected) throw new TileError(`tile ${key} holds ${n} points, the manifest says ${expected}`);
  if (buf.byteLength !== HEADER + n * (8 + CAT_BYTES)) throw new TileError(`tile ${key} is ${buf.byteLength} bytes, expected ${HEADER + n * 12}`);
  const ids = new Uint32Array(buf, HEADER, n);
  const q = new Uint16Array(buf, HEADER + 4 * n, 2 * n);
  const cats = new Uint8Array(buf, HEADER + 8 * n, CAT_BYTES * n);
  const wx = new Float32Array(n), wy = new Float32Array(n), codes = new Uint32Array(n);
  const step = sq.side / Q;
  let prev = 0;
  for (let i = 0; i < n; i++) {
    const qx = q[2 * i], qy = q[2 * i + 1];
    wx[i] = sq.x + qx * step;
    wy[i] = sq.y + qy * step;
    const code = morton(qx, qy);
    if (code < prev) throw new TileError(`tile ${key} is not in Morton order`);
    codes[i] = prev = code;
  }
  return { key, z, n, ids, q, cats, wx, wy, codes, ...sq };
}
