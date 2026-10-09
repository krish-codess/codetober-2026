// The shape of data/manifest.json, written by dots/tiles.py.

export interface Interval {
  mean: number;
  lo: number;
  hi: number;
  n: number;
}

export interface VariantMetrics {
  recall: { all: Interval; fitted: Interval | null; placed: Interval | null };
  purity: Record<string, Interval>;
  recall_vs_pca: Interval | null;
}

/** [z, x, y, points] */
export type TileRef = [number, number, number, number];

export interface Variant {
  name: string;
  label: string;
  params: Record<string, unknown>;
  tiles_path: string;
  /** x0, y0, side of the root square, in projection units */
  bounds: [number, number, number];
  /** x0, y0, x1, y1 holding the middle 99% of points: the opening view */
  view: [number, number, number, number];
  tiles: TileRef[];
  n_points: number;
  dropped_non_finite: number;
  density_grid: number;
  bytes: number;
  metrics: VariantMetrics | null;
}

export interface Column {
  name: string;
  /** index = category code stored in the tiles; 0 is "(none)" */
  labels: string[];
  counts: number[];
}

export interface Manifest {
  format: number;
  build_id: string;
  created_at: string;
  source: {
    name: string;
    synthetic: boolean;
    url: string | null;
    lines: number;
    accepted: number;
    quarantined: number;
    quarantine_reasons: Record<string, number>;
  };
  n_points: number;
  tile_capacity: number;
  columns: Column[];
  meta: { path: string; bytes: number; rows: number };
  eval: { k?: number; chance?: { recall: number; purity: Record<string, number> }; high_dim_purity?: Record<string, Interval> };
  variants: Variant[];
}

export const FORMAT = 1;

/** Reject a manifest this build cannot draw, with a message a person can act on. */
export function checkManifest(m: unknown): Manifest {
  const x = m as Partial<Manifest> | null;
  if (!x || typeof x !== "object" || !Array.isArray(x.variants) || !Array.isArray(x.columns) || typeof x.build_id !== "string")
    throw new Error("The data manifest is not in the expected shape.");
  if (x.format !== FORMAT) throw new Error(`The data is in format ${x.format}; this page reads format ${FORMAT}. Reload to get the matching page.`);
  return x as Manifest;
}
