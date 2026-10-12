// Typed client for the API. Shapes mirror docs/openapi.json, which is generated from the server code.

export type Estimate = { estimate: number; ci95: [number, number] };

export type SplitMetrics = {
  qini: number;
  qini_ci95: [number, number];
  auuc: number;
  uplift_at: Record<string, number>;
  p_beats_response?: number;
  p_beats_random?: number;
  qini_share_of_oracle?: number;
  pehe?: number;
};

export type Run = {
  id: string;
  learner: string;
  status: "complete" | "failed";
  error: string | null;
  is_champion: boolean;
  scored: boolean;
  fit_seconds: number | null;
  metrics: { estimates_uplift: boolean; val: SplitMetrics; test: SplitMetrics; calibration_slope: number | null } | null;
  curves: { qini: [number, number][]; uplift: [number, number][] } | null;
};

export type Experiment = {
  id: string;
  dataset: string;
  outcome: string;
  stale: boolean;
  split_strategy: string | null;
  finished_at: string | null;
  code_version: string;
  audit: {
    units: number;
    treated_share: number;
    srm_p_value: number;
    treatment_auc: number;
    reference?: { oracle_qini_test: number | null; champion_beats_response_on_val: boolean };
  } | null;
  runs: Run[];
};

export type Dataset = {
  name: string;
  outcome: string;
  units: number;
  quarantined: number;
  quarantine_reasons: Record<string, number>;
  experiment_id: string | null;
  trained_at: string | null;
  stale: boolean;
};

export type Decile = {
  decile: number;
  n_treated: number;
  n_control: number;
  mean_predicted: number;
  observed_uplift: number;
  ci_low: number;
  ci_high: number;
  true_uplift: number | null;
};

export type Verdict = "helps" | "hurts" | "unclear";

export type Group = {
  n: number;
  share_of_population: number;
  mean_predicted: number | null;
  observed_uplift: number;
  ci_low: number | null;
  ci_high: number | null;
  true_uplift: number | null;
  verdict: Verdict;
  traits: { feature: string; level: string; share_in_group: number; share_overall: number }[];
};

export type Profiles = { high_uplift: Group; negative_uplift?: Group | null };

export type Segment = {
  definition: string;
  n_treated: number;
  n_control: number;
  uplift: number;
  ci_low: number;
  ci_high: number;
  p_adjusted: number;
  verdict: Verdict;
};

export type Page<T> = { items: T[]; next_cursor: string | null };

export type Policy = {
  id: string;
  budget: string;
  cost_per_treatment: string;
  value_per_outcome: string;
  population: number;
  n_targeted: number;
  spend: string;
  min_uplift: number | null;
  evaluation:
    | { evaluable: false; reason: string }
    | {
        evaluable: true;
        profit_per_unit: { policy: number; treat_all: number; treat_none: number };
        policy_minus_treat_all: Estimate;
        policy_minus_treat_none: Estimate;
        share_of_population_treated: number;
        share_of_treat_all_effect_captured: number | null;
      };
};

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
  ) {
    super(message);
  }
  /** Worth retrying without the user doing anything differently. */
  get transient(): boolean {
    return this.status === 0 || this.status >= 500;
  }
}

const TOKEN_KEY = "uplift-token";
export const getToken = (): string => sessionStorage.getItem(TOKEN_KEY) ?? "";
export const setToken = (token: string): void => (token ? sessionStorage.setItem(TOKEN_KEY, token) : sessionStorage.removeItem(TOKEN_KEY));

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers: { ...init.headers, Authorization: `Bearer ${getToken()}` } });
  } catch {
    throw new ApiError(0, "network", "The server cannot be reached. Check your connection.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const error = body?.error;
    throw new ApiError(response.status, error?.code ?? "http_error", error?.message ?? `The server answered ${response.status}.`, error?.request_id);
  }
  return response;
}

export const getJson = async <T>(path: string): Promise<T> => (await request(path)).json();

export async function createPolicy(body: { run_id: string; budget: number; cost_per_treatment: number; value_per_outcome: number }, key: string): Promise<Policy> {
  const response = await request("/v1/policies", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": key },
    body: JSON.stringify(body),
  });
  return response.json();
}

export async function downloadPolicy(id: string): Promise<void> {
  const blob = await (await request(`/v1/policies/${id}/export`)).blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `policy-${id}.csv`;
  link.click();
  URL.revokeObjectURL(link.href);
}
