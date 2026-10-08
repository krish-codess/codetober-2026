import { useCallback, useEffect, useState } from "react";

// Shapes mirror squeeze/schemas.py (docs/openapi.json is generated from the same models).
export type Bench = {
  runs: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  top1_device: number;
  agree_host: number;
  load_w: number | null;
  idle_w: number | null;
  energy_mj: number | null;
  power_source: string | null;
  power_unavailable: string | null;
  cpu_model: string;
  threads: number;
  last_measured_at: string;
};
export type Point = {
  name: string;
  parent: string | null;
  technique: string;
  arch: string;
  precision: "fp32" | "int8" | "int8+fp32";
  params: number;
  size_bytes: number;
  top1: number;
  top1_lo: number;
  top1_hi: number;
  n_eval: number;
  top1_deploy: number;
  parent_agree: number | null;
  parent_delta: number | null;
  parent_delta_lo: number | null;
  parent_delta_hi: number | null;
  gate: "pass" | "fail";
  gate_reason: string;
  bench: Bench | null;
  pareto: boolean;
  meets_budget: boolean | null;
};
export type Target = {
  name: string;
  label: string;
  arch: string;
  runtimes: string[];
  threads: number;
  budget_p95_ms: number | null;
  power_sensor: string;
  results: number;
  packages: number;
};
export type Tradeoff = {
  target: Target;
  runtime: string;
  runtimes_measured: string[];
  run: { run_id: string; created_at: string; git_commit: string; dataset_version: string; teacher_top1: number; budget_pt: number } | null;
  baselines: { name: string; top1: number }[];
  points: Point[];
};
export type Sensitivity = {
  run_id: string;
  variant: string;
  rows: { rank: number; node: string; op_type: string; kl: number; top1_drop: number; kept_float: boolean }[];
};
export type Package = {
  package_id: string;
  name: string;
  target: string;
  run_id: string;
  filename: string;
  size_bytes: number;
  git_commit: string;
  dataset_version: string;
  hosted: boolean;
  variants: { name: string; precision: string; size_bytes: number; top1_host: number }[];
};

type Entry = { data: unknown; etag: string | null; at: number };
export type Resource<T> = {
  data: T | null;
  /** Set when the last request failed. With `data` also set, the data is stale. */
  error: string | null;
  loading: boolean;
  fetchedAt: number | null;
  refresh: () => void;
};

// Deliberate caching: one entry per URL, revalidated with If-None-Match (a 304 costs the server
// one small query). Entries are mirrored to localStorage so a failed request can fall back to
// the last good answer, clearly marked stale.
const memory = new Map<string, Entry>();
const KEY = "squeeze:";

function recall(url: string): Entry | undefined {
  const hit = memory.get(url);
  if (hit) return hit;
  try {
    const stored = localStorage.getItem(KEY + url);
    if (stored) {
      const entry = JSON.parse(stored) as Entry;
      memory.set(url, entry);
      return entry;
    }
  } catch {
    // storage unavailable or corrupt: behave as if nothing was cached
  }
  return undefined;
}

export function clearCache(): void {
  memory.clear();
}

async function load(url: string): Promise<Entry> {
  const cached = recall(url);
  const response = await fetch(url, { headers: cached?.etag ? { "if-none-match": cached.etag } : {} });
  if (response.status === 304 && cached) {
    cached.at = Date.now();
    return cached;
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.error?.message) message = `${body.error.message} (request ${body.request_id})`;
    } catch {
      // not our error shape (a proxy, perhaps): the status line is all there is
    }
    throw Object.assign(new Error(message), { status: response.status });
  }
  const entry = { data: await response.json(), etag: response.headers.get("etag"), at: Date.now() };
  memory.set(url, entry);
  try {
    localStorage.setItem(KEY + url, JSON.stringify(entry));
  } catch {
    // over quota: the in-memory copy still serves this session
  }
  return entry;
}

/** `url: null` means "not needed yet". A 404 is an answer (nothing there), not a stale-data case. */
export function useResource<T>(url: string | null): Resource<T> {
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<{ url: string | null; entry?: Entry; error: string | null; loading: boolean }>({
    url,
    entry: url ? recall(url) : undefined,
    error: null,
    loading: url !== null,
  });
  useEffect(() => {
    if (url === null) return;
    let live = true;
    setState({ url, entry: recall(url), error: null, loading: true });
    load(url).then(
      (entry) => live && setState({ url, entry, error: null, loading: false }),
      (err: Error & { status?: number }) =>
        live && setState({ url, entry: err.status === 404 ? undefined : recall(url), error: err.message, loading: false }),
    );
    return () => {
      live = false;
    };
  }, [url, tick]);
  const refresh = useCallback(() => setTick((t) => t + 1), []);
  const current = state.url === url ? state : { entry: url ? recall(url) : undefined, error: null, loading: url !== null };
  return {
    data: (current.entry?.data as T | undefined) ?? null,
    error: current.error,
    loading: current.loading,
    fetchedAt: current.entry?.at ?? null,
    refresh,
  };
}
