// The live fleet as flat typed arrays, so tens of thousands of vessels cost one buffer upload per
// frame and no per-vessel objects. Also the small pure helpers the views share.

import type { LiveRow, TrackPoint } from "./api";

const KNOT_MS = 1852 / 3600;
const M_PER_DEG = 111_320;
/** Never extrapolate a vessel further than this past its last fix: a guess must not outlive its evidence. */
export const MAX_EXTRAPOLATE_S = 120;

export class Fleet {
  size = 0;
  mmsi = new Int32Array(1024);
  fixTime = new Float64Array(1024);
  sog = new Float32Array(1024);
  private base = new Float64Array(2048);
  private velocity = new Float64Array(2048); // degrees per second
  private index = new Map<number, number>();

  /** Insert or update. Rows older than what we hold are ignored: late arrivals never move a vessel backwards. */
  upsert(rows: LiveRow[]): void {
    for (const [mmsi, lon, lat, sog, cog, , t] of rows) {
      let i = this.index.get(mmsi);
      if (i === undefined) {
        if (this.size === this.mmsi.length) this.grow();
        i = this.size++;
        this.index.set(mmsi, i);
        this.mmsi[i] = mmsi;
      } else if (t < this.fixTime[i]) continue;
      this.fixTime[i] = t;
      this.sog[i] = sog ?? 0;
      this.base[2 * i] = lon;
      this.base[2 * i + 1] = lat;
      const speed = sog !== null && cog !== null && sog >= 0.5 ? sog * KNOT_MS : 0;
      const heading = ((cog ?? 0) * Math.PI) / 180;
      this.velocity[2 * i] = (speed * Math.sin(heading)) / (M_PER_DEG * Math.max(0.05, Math.cos((lat * Math.PI) / 180)));
      this.velocity[2 * i + 1] = (speed * Math.cos(heading)) / M_PER_DEG;
    }
  }

  /** Dead-reckoned [lon, lat] pairs at time `now` (unix seconds), written into `out`. */
  positionsAt(now: number, out: Float32Array): Float32Array {
    for (let i = 0; i < this.size; i++) {
      const dt = Math.min(MAX_EXTRAPOLATE_S, Math.max(0, now - this.fixTime[i]));
      out[2 * i] = this.base[2 * i] + this.velocity[2 * i] * dt;
      out[2 * i + 1] = this.base[2 * i + 1] + this.velocity[2 * i + 1] * dt;
    }
    return out;
  }

  private grow(): void {
    const widen = <T extends Int32Array | Float64Array | Float32Array>(a: T): T => {
      const b = new (a.constructor as new (n: number) => T)(a.length * 2);
      b.set(a);
      return b;
    };
    this.mmsi = widen(this.mmsi);
    this.fixTime = widen(this.fixTime);
    this.sog = widen(this.sog);
    this.base = widen(this.base);
    this.velocity = widen(this.velocity);
  }
}

/** Where along a track a vessel was at time t: [lon, lat], linearly interpolated, clamped to the ends. */
export function positionAt(points: TrackPoint[], t: number): [number, number] | null {
  if (points.length === 0) return null;
  let lo = 0;
  let hi = points.length - 1;
  if (t <= points[lo][0]) return [points[lo][1], points[lo][2]];
  if (t >= points[hi][0]) return [points[hi][1], points[hi][2]];
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] <= t) lo = mid;
    else hi = mid;
  }
  const [t0, x0, y0] = points[lo];
  const [t1, x1, y1] = points[hi];
  const f = (t - t0) / (t1 - t0);
  return [x0 + (x1 - x0) * f, y0 + (y1 - y0) * f];
}

/** Split a track into drawable paths wherever the vessel was silent longer than `gapS`. */
export function segments(points: TrackPoint[], gapS: number): [number, number][][] {
  const out: [number, number][][] = [];
  let current: [number, number][] = [];
  points.forEach((p, i) => {
    if (i > 0 && p[0] - points[i - 1][0] > gapS) {
      out.push(current);
      current = [];
    }
    current.push([p[1], p[2]]);
  });
  if (current.length) out.push(current);
  return out;
}

export function duration(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  const d = Math.floor(minutes / 1440);
  const h = Math.floor((minutes % 1440) / 60);
  const m = minutes % 60;
  if (d) return `${d} d ${h} h`;
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`;
}

export function utc(iso: string | number): string {
  const d = new Date(typeof iso === "number" ? iso * 1000 : iso);
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function ago(seconds: number): string {
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))} s ago`;
  return `${duration(seconds)} ago`;
}

export const CLASS_LABEL = { dark: "Went dark", coverage_gap: "Coverage gap", position_jump: "Position jump" } as const;
/** A glyph per class, so the class is readable without colour. */
export const CLASS_GLYPH = { dark: "●", coverage_gap: "○", position_jump: "↯" } as const;
