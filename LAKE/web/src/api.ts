// Data access. One rule: nothing fetches on render. Results are fetched once, cached with
// their ETag, and revalidated (a 304 costs no body) on demand or when the tab regains focus.
import { useCallback, useEffect, useRef, useState } from "react";

export interface Stat { n: number; median_s: number; min_s: number; max_s: number; stdev_s: number; cv: number }
export interface Cell {
  ok: boolean; cold?: Stat; warm?: Stat; cpu_s?: number;
  bytes_read?: number; reads?: number; read_fraction?: number; error?: string;
}
export interface Variant {
  id: string; format: "csv" | "parquet" | "orc" | "avro"; codec: string; level: number | null;
  chunk: number | null; layout: string; reader: string; bytes: number; rows: number;
  write_s: number; write_cpu_s: number; chunks: number | null; ratio_vs_csv: number;
  queries: Record<string, Cell>;
}
export interface QueryDef { id: string; class: string; sql: string; description: string; control?: string | null }
export interface Pushdown {
  variant: string; format: string; reader: string;
  read_fraction: Record<string, number>; pruned_vs_control: Record<string, number>;
  projection_pushdown: boolean | null; predicate_pushdown: boolean | null;
}
export interface LabColumn { column: string; type: string; distinct: number; null_frac: number; raw_bytes: number }
export interface LabCell { column: string; variant: string; format: string; codec: string; bytes: number; ratio: number }
export interface Results {
  generated_at: string; origin: "local" | "published"; partial: boolean; missing: string[];
  dataset: {
    name: string; rows: number; csv_bytes: number; landed: number; columns: number; sort_key: string;
    source: string; months: string[]; quarantined: Record<string, number>; warnings: Record<string, number>;
  };
  env: {
    os?: string; cpu?: string; logical_cpus?: number; ram_gb?: number; in_container?: boolean;
    versions?: Record<string, string>;
    eviction?: { supported: boolean; effective: boolean; cached_mb_s: number; evicted_mb_s: number };
    settings?: { cold_runs: number; warmups: number; warm_runs: number; cell_budget_s: number };
  };
  queries: QueryDef[]; variants: Variant[]; pushdown: Pushdown[];
  columns: { lab: { rows: number; columns: LabColumn[]; cells: LabCell[] } };
  stages: Record<string, { seconds: number; budget_s?: number; rows?: number; rows_per_s?: number }>;
}
export interface Ranked {
  variant: string; format: string; codec: string; storage: number; scan: number; compute: number;
  requests: number; write: number; total: number; latency_s: number; score: number;
}
export interface Recommendation {
  pick: string; why: string[]; ranked: Ranked[]; excluded: { variant: string; reason: string }[];
  assumptions: string[]; price: { label: string }; workload: { mix: Record<string, number> };
}
export interface Workload {
  dataset_gb: number; queries_per_month: number; mix: Record<string, number>; rewrites_per_month: number;
  provider: "aws" | "gcp" | "azure"; engine: "serverless" | "self_hosted"; objective: "cost" | "speed" | "balanced";
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details: { field: string; problem: string }[] = []) {
    super(message);
  }
}

async function fail(r: Response): Promise<never> {
  const body = await r.json().catch(() => null);
  const e = body?.error;
  throw new ApiError(r.status, e?.code ?? "http_error", e?.message ?? `The server answered ${r.status}.`, e?.details);
}

/** Read a body while reporting real progress (bytes received / Content-Length). */
async function readJson(r: Response, onProgress: (fraction: number) => void): Promise<unknown> {
  const total = Number(r.headers.get("content-length"));
  if (!r.body || !total) return r.json();
  const reader = r.body.getReader();
  const chunks: Uint8Array[] = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    onProgress(Math.min(1, received / total));
  }
  const all = new Uint8Array(received);
  let offset = 0;
  for (const c of chunks) { all.set(c, offset); offset += c.length; }
  return JSON.parse(new TextDecoder().decode(all));
}

const STORE = "bakeoff.results.v1";
let memory: { etag: string; data: Results } | null = null;

function cached(): { etag: string; data: Results } | null {
  if (memory) return memory;
  try { memory = JSON.parse(localStorage.getItem(STORE) ?? "null"); } catch { memory = null; }
  return memory;
}

export type ResultsState =
  | { status: "loading"; progress: number }
  | { status: "empty"; message: string }
  | { status: "error"; message: string }
  // staleReason set = we are showing the cached copy because the refresh failed
  | { status: "ready"; data: Results; staleReason: string | null; checkedAt: number };

export function useResults(): { state: ResultsState; refresh: () => void; refreshing: boolean } {
  const [state, setState] = useState<ResultsState>({ status: "loading", progress: 0 });
  const [refreshing, setRefreshing] = useState(false);
  const inflight = useRef<AbortController | null>(null);
  const lastCheck = useRef(0);

  const refresh = useCallback(() => {
    inflight.current?.abort();
    const ctl = new AbortController();
    inflight.current = ctl;
    lastCheck.current = Date.now();
    const have = cached();
    if (have) setState({ status: "ready", data: have.data, staleReason: null, checkedAt: Date.now() });
    setRefreshing(true);
    fetch("/api/results", { signal: ctl.signal, headers: have ? { "If-None-Match": have.etag } : {} })
      .then(async (r) => {
        if (r.status === 304 && have) return have.data;
        if (!r.ok) await fail(r);
        const data = (await readJson(r, (p) => { if (!have) setState({ status: "loading", progress: p }); })) as Results;
        memory = { etag: r.headers.get("etag") ?? "", data };
        try { localStorage.setItem(STORE, JSON.stringify(memory)); } catch { /* quota or private mode: memory cache still works */ }
        return data;
      })
      .then((data) => setState({ status: "ready", data, staleReason: null, checkedAt: Date.now() }))
      .catch((e: unknown) => {
        if (ctl.signal.aborted) return;
        const message = e instanceof Error ? e.message : "Network error";
        if (e instanceof ApiError && e.code === "no_results") {
          memory = null;
          try { localStorage.removeItem(STORE); } catch { /* ignore */ }
          setState({ status: "empty", message });
        } else if (have) {
          setState({ status: "ready", data: have.data, staleReason: message, checkedAt: Date.now() });
        } else {
          setState({ status: "error", message });
        }
      })
      .finally(() => { if (!ctl.signal.aborted) setRefreshing(false); });
  }, []);

  useEffect(() => {
    refresh();
    // Deliberate invalidation: revalidate when the user comes back, at most once a minute.
    const onFocus = () => { if (Date.now() - lastCheck.current > 60_000) refresh(); };
    window.addEventListener("focus", onFocus);
    return () => { window.removeEventListener("focus", onFocus); inflight.current?.abort(); };
  }, [refresh]);

  return { state, refresh, refreshing };
}

const recommendations = new Map<string, Recommendation>();

/** Pure on the server, so identical requests are answered from memory. Cleared when results change. */
export async function recommend(w: Workload, resultsStamp: string, signal: AbortSignal): Promise<Recommendation> {
  const key = resultsStamp + JSON.stringify(w);
  const hit = recommendations.get(key);
  if (hit) return hit;
  const r = await fetch("/api/recommend", {
    method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(w),
  });
  if (!r.ok) await fail(r);
  const out = (await r.json()) as Recommendation;
  recommendations.set(key, out);
  return out;
}

export function resetCachesForTests(): void {
  memory = null;
  recommendations.clear();
  try { localStorage.removeItem(STORE); } catch { /* ignore */ }
}

// ---- formatting, shared by every panel
export const fmtBytes = (b: number): string =>
  b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(b >= 1e8 ? 0 : 1)} MB` : `${(b / 1e3).toFixed(0)} kB`;
export const fmtSeconds = (s: number): string =>
  s >= 10 ? `${s.toFixed(1)} s` : s >= 1 ? `${s.toFixed(2)} s` : `${(s * 1000).toFixed(s >= 0.1 ? 0 : 1)} ms`;
export const fmtPct = (f: number): string => `${(f * 100).toFixed(f < 0.1 ? 1 : 0)}%`;
export const fmtUsd = (d: number): string =>
  d >= 100 ? `$${Math.round(d).toLocaleString("en-US")}` : d >= 0.01 || d === 0 ? `$${d.toFixed(2)}` : `$${d.toFixed(4)}`;
