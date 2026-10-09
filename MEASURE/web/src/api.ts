// The HTTP contract, as the UI uses it. Shapes mirror the API's generated OpenAPI document.

export type Claim = { metric: string; top_permille: number; text: string; basis: string };
export type Stat = { label: string; value: string };
export type Card = {
  type: string;
  family: string;
  shareable: boolean;
  headline: string;
  value: string;
  unit: string;
  body: string;
  claim: Claim | null;
  stats?: Stat[] | null;
};
export type Wrapped = {
  version: number;
  year: number;
  user: { id: number; login: string };
  tier: string;
  archetype: string;
  population: { users: number; basis: string };
  cards: Card[];
  run_id: string;
  generated_at: string;
};
export type Share = { share_id: string; card_type: string; url: string; image_url: string; created: boolean };
export type SuperlativeRow = { card_type: string; family: string; users: number; share_of_users: number };
export type Superlatives = { run_id: string; users: number; cards: SuperlativeRow[] };
export type ShareRateRow = { card_type: string; viewers: number; sharers: number; share_rate: number | null };

/** status 0 means the request never got an answer (offline, DNS, server down). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
  }
  /** Worth trying again without the user changing anything. */
  get transient(): boolean {
    return this.status === 0 || this.status >= 500;
  }
}

async function request(path: string, token: string, init: RequestInit = {}): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { Authorization: `Bearer ${token}`, ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
    });
  } catch {
    throw new ApiError(0, "network", "We could not reach the server. Check your connection.");
  }
  if (response.ok || response.status === 304) return response;
  // Errors are JSON in a fixed shape; anything else (a proxy's HTML page) still becomes a usable error.
  const body = await response.json().catch(() => null);
  const error = body?.error;
  throw new ApiError(response.status, error?.code ?? "http_error", error?.message ?? `Request failed (${response.status}).`, error?.request_id);
}

/** Returns null when the server says the copy identified by `etag` is still current. */
export async function fetchWrapped(
  token: string,
  etag?: string,
  onHeaders?: () => void,
): Promise<{ wrapped: Wrapped; etag: string } | null> {
  const response = await request("/v1/wrapped", token, { headers: etag ? { "If-None-Match": etag } : {} });
  onHeaders?.();
  if (response.status === 304) return null;
  return { wrapped: (await response.json()) as Wrapped, etag: response.headers.get("ETag") ?? "" };
}

/** Best effort: analytics must never get in the way of the story. The server makes it idempotent. */
export function recordView(token: string, cardType: string): void {
  request(`/v1/wrapped/views/${encodeURIComponent(cardType)}`, token, { method: "PUT", keepalive: true }).catch(() => {});
}

export async function createShare(token: string, cardType: string): Promise<Share> {
  const response = await request("/v1/wrapped/shares", token, { method: "POST", body: JSON.stringify({ card_type: cardType }) });
  return (await response.json()) as Share;
}

export async function fetchAnalytics(adminToken: string): Promise<{ superlatives: Superlatives; shareRate: ShareRateRow[] }> {
  const [superlatives, shareRate] = await Promise.all([
    request("/v1/admin/analytics/superlatives", adminToken).then((r) => r.json()),
    request("/v1/admin/analytics/share-rate", adminToken).then((r) => r.json()),
  ]);
  return { superlatives, shareRate };
}
