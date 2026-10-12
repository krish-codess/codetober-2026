// Typed client for the API. Shapes mirror docs/openapi.json.

export type Classification = "dark" | "coverage_gap" | "position_jump";

/** [mmsi, lon, lat, sog_kn, cog_deg, heading_deg, fix_time_unix_s] */
export type LiveRow = [number, number, number, number | null, number | null, number | null, number];
export interface Live {
  server_time: string;
  data_time: string | null;
  truncated: boolean;
  vessels: LiveRow[];
}
export interface DarkEvent {
  id: number;
  mmsi: number;
  name: string | null;
  flag: string | null;
  classification: Classification;
  score: number;
  impossible_speed: boolean;
  started_at: string;
  ended_at: string | null;
  duration_s: number;
  start: [number, number];
  end: [number, number] | null;
  distance_nm: number | null;
  implied_speed_kn: number | null;
  start_reliability: number;
  end_reliability: number | null;
  neighbours_heard: number;
  reasons: string[];
}
export interface Vessel {
  mmsi: number;
  name: string | null;
  callsign: string | null;
  imo: number | null;
  ship_type: number | null;
  length_m: number | null;
  beam_m: number | null;
  flag: string | null;
  country: string | null;
  first_seen: string;
  last_seen: string | null;
  lon: number | null;
  lat: number | null;
  sog: number | null;
  events: number;
}
/** [fix_time_unix_s, lon, lat, sog_kn, cog_deg] */
export type TrackPoint = [number, number, number, number | null, number | null];
export interface Stats {
  vessels: number;
  positions_estimate: number;
  newest_position_at: string | null;
  events: Record<string, number>;
  quarantine: Record<string, number>;
}
export interface Coverage {
  cell_deg: number | null;
  cells: [number, number, number][];
  truncated: boolean;
}
export interface VesselHit {
  mmsi: number;
  name: string | null;
  flag: string | null;
}

/** An error the user can act on: what went wrong, and the id to quote if they report it. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly requestId?: string,
  ) {
    super(message);
  }
}

const BASE = import.meta.env.VITE_API_BASE ?? "";

export async function get<T>(path: string, params: Record<string, string | number | boolean | undefined> = {}, signal?: AbortSignal): Promise<T> {
  const query = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") query.set(k, String(v));
  const qs = query.toString();
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}${qs ? `?${qs}` : ""}`, { signal });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError("Cannot reach the server. Check your connection.", 0);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const fallback = response.status === 503 ? "The service is temporarily unavailable." : `Request failed (${response.status}).`;
    throw new ApiError(body?.error?.message ?? fallback, response.status, body?.error?.request_id);
  }
  return response.json() as Promise<T>;
}

/** Walk every page of a vessel's track, reporting progress. Returns what it got even if a later page fails. */
export async function fetchTrack(
  mmsi: number,
  onProgress: (points: number) => void,
  signal?: AbortSignal,
): Promise<{ points: TrackPoint[]; error: ApiError | null }> {
  const points: TrackPoint[] = [];
  let cursor: string | undefined;
  do {
    try {
      const page = await get<{ points: TrackPoint[]; next_cursor: string | null }>(`/api/v1/vessels/${mmsi}/track`, { limit: 5000, cursor }, signal);
      points.push(...page.points);
      cursor = page.next_cursor ?? undefined;
      onProgress(points.length);
    } catch (e) {
      if ((e as Error).name === "AbortError" || points.length === 0) throw e;
      return { points, error: e as ApiError };
    }
  } while (cursor);
  return { points, error: null };
}
