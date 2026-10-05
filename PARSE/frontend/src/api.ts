// Thin typed client. Types mirror the OpenAPI document served at /api/v1/openapi.json.

export type Role = "viewer" | "annotator" | "admin";
export interface Principal {
  name: string;
  role: Role;
}
export interface TaxNode {
  id: number;
  parent_id: number | null;
  name: string;
  title: string;
  path: string;
  depth: number;
  n_labels: number;
  n_review: number;
}
export interface Taxonomy {
  version: number;
  nodes: TaxNode[];
}
export interface Suggestion {
  node_id: number;
  prob: number;
  selected: boolean;
}
export interface QueueItem {
  id: number;
  text: string;
  lang: string | null;
  source: string;
  created_at: string | null;
  is_late: boolean;
  split: "pool" | "test";
  suggestions: Suggestion[];
  confidence: number | null;
  uncertainty: number | null;
  scored_by_model: number | null;
  current_node_ids: number[];
  review_reason: string | null;
}
export interface QueuePage {
  items: QueueItem[];
  next_cursor: string | null;
  active_model: number | null;
  remaining: number;
}
export interface Summary {
  n: number;
  hf1: number;
  precision: number;
  recall: number;
  exact_match: number;
  macro_f1: number;
  hf1_ci95?: [number, number];
}
export interface NodeMetric {
  node_id: number;
  path: string;
  title: string;
  depth: number;
  parent_id: number | null;
  support: number;
  predicted: number;
  precision: number | null;
  recall: number | null;
  f1: number;
  precision_ci: [number, number];
  recall_ci: [number, number];
}
export interface RoutingRow {
  threshold: number;
  coverage: number;
  exact_match: number | null;
  n: number;
}
export interface NodeMetrics {
  model_version: number;
  lang: string;
  languages: string[];
  overall: Summary;
  by_lang: Record<string, Summary>;
  calibration: {
    ece_item?: number;
    ece_item_uncalibrated?: number;
    ece_label?: number;
    ece_domain?: number;
    routing?: RoutingRow[];
    label_routing?: { threshold: number; n: number; precision: number | null; recall: number }[];
    domain_routing?: { threshold: number; n: number; coverage: number; accuracy: number | null }[];
  };
  nodes: NodeMetric[];
}
export interface EfficiencyPoint {
  model_version: number;
  n_labeled: number;
  hf1: number;
  hf1_ci95: [number, number] | null;
  status: string;
}
export interface SimCurvePoint {
  n_labeled: number;
  hf1_mean: number;
  hf1_sd: number;
}
export interface Efficiency {
  live: EfficiencyPoint[];
  simulation: {
    full_pool: { n_labeled: number; hf1: number };
    curves: Record<string, SimCurvePoint[]>;
    labels_to_reach_fraction_of_full: Record<string, Record<string, number | null>>;
  } | null;
}
export interface Job {
  id: number;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed";
  progress: number;
  stage: string;
  attempts: number;
  error: string | null;
  result: Record<string, unknown> | null;
  requested_by: string;
  requested_at: string;
}
export interface Stats {
  pool: number;
  test: number;
  labelled: number;
  needs_review: number;
  quarantined: Record<string, number>;
  late: number;
  text_duplicates: number;
  by_lang: Record<string, { pool: number; labelled: number }>;
  taxonomy_version: number | null;
  active_model: number | null;
}
export interface Change {
  version: number;
  op: string;
  params: Record<string, unknown>;
  labels_remapped: number;
  labels_flagged: number;
  replayed: boolean;
  actor: string | null;
  created_at: string | null;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
  ) {
    super(message);
  }
}

const TOKEN_KEY = "parse.token";
// sessionStorage: the token dies with the tab and is never written to disk-backed localStorage.
export const getToken = () => sessionStorage.getItem(TOKEN_KEY);
export const setToken = (t: string | null) =>
  t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY);

export async function api<T>(path: string, init: RequestInit & { idempotencyKey?: string } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  if (init.idempotencyKey) headers.set("Idempotency-Key", init.idempotencyKey);
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the server. Check your connection and try again.");
  }
  if (res.ok) return (await res.json()) as T;
  let code = "http_error";
  let message = `Request failed (${res.status}).`;
  let requestId: string | undefined;
  try {
    const body = await res.json();
    code = body.error.code;
    message = body.error.message;
    requestId = body.error.request_id;
  } catch {
    // non-JSON error body (e.g. a proxy 502): keep the generic message
    if (res.status >= 502) message = "The server is not responding. It may be restarting; try again shortly.";
  }
  throw new ApiError(res.status, code, message, requestId);
}

export const newKey = () => crypto.randomUUID();
export const pct = (v: number | null | undefined, digits = 0) => (v == null ? "–" : `${(v * 100).toFixed(digits)}%`);
