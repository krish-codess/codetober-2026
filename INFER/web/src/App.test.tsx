import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { Decile, Experiment, Policy, Run, Segment } from "./api";
import { tuning } from "./useResource";

// ---- a small, fixed world

const curve = (bend: number): [number, number][] => Array.from({ length: 101 }, (_, i) => [i / 100, 0.06 * (i / 100) ** bend]);
const run = (learner: string, bend: number, extra: Partial<Run> = {}): Run => ({
  id: `run-${learner}`,
  learner,
  status: "complete",
  error: null,
  is_champion: false,
  scored: false,
  fit_seconds: 1,
  metrics: {
    estimates_uplift: !["random", "response"].includes(learner),
    calibration_slope: 0.95,
    val: { qini: 0.01, qini_ci95: [0.005, 0.015], auuc: 0.01, uplift_at: { "0.2": 0.1 } },
    test: { qini: 0.012 * (1 - bend), qini_ci95: [0.001, 0.02], auuc: 0.01, uplift_at: { "0.2": 0.12 }, p_beats_response: 0.97, qini_share_of_oracle: 0.9 },
  },
  curves: { qini: curve(bend), uplift: curve(bend) },
  ...extra,
});
const experiment = (extra: Partial<Experiment> = {}): Experiment => ({
  id: "exp-1",
  dataset: "synthetic",
  outcome: "visit",
  stale: false,
  split_strategy: "temporal",
  finished_at: "2026-10-10T10:00:00+00:00",
  code_version: "abc1234",
  audit: { units: 1000, treated_share: 0.667, srm_p_value: 0.61, treatment_auc: 0.501, reference: { oracle_qini_test: 0.01, champion_beats_response_on_val: true } },
  runs: [run("x_learner", 0.4, { is_champion: true, scored: true }), run("t_learner", 0.5), run("response", 0.95), run("random", 1)],
  ...extra,
});
const deciles: Decile[] = Array.from({ length: 10 }, (_, i) => ({
  decile: i + 1,
  n_treated: 200,
  n_control: 100,
  mean_predicted: 0.15 - i * 0.02,
  observed_uplift: 0.14 - i * 0.02,
  ci_low: 0.1 - i * 0.02,
  ci_high: 0.18 - i * 0.02,
  true_uplift: 0.15 - i * 0.021,
}));
const group = (uplift: number, verdict: "helps" | "hurts") => ({
  n: 300, share_of_population: 0.1, mean_predicted: uplift, observed_uplift: uplift, ci_low: uplift - 0.02, ci_high: uplift + 0.02, true_uplift: uplift, verdict,
  traits: [{ feature: "channel", level: "Phone", share_in_group: 0.9, share_overall: 0.44 }],
}); // prettier-ignore
const segment = (definition: string, uplift: number, verdict: Segment["verdict"]): Segment => ({
  definition, uplift, verdict, n_treated: 900, n_control: 450, ci_low: uplift - 0.02, ci_high: uplift + 0.02, p_adjusted: 0.0004,
}); // prettier-ignore
const policy: Policy = {
  id: "pol-1", budget: "1500.00", cost_per_treatment: "1.0000", value_per_outcome: "20.0000", population: 8000, n_targeted: 1500, spend: "1500.0000", min_uplift: 0.11,
  evaluation: {
    evaluable: true,
    profit_per_unit: { policy: 3.61, treat_all: 3.2, treat_none: 3.0 },
    policy_minus_treat_all: { estimate: 0.41, ci95: [0.1, 0.7] },
    policy_minus_treat_none: { estimate: 0.61, ci95: [0.3, 0.9] },
    share_of_population_treated: 0.1875,
    share_of_treat_all_effect_captured: 0.46,
  },
}; // prettier-ignore

// ---- a fake server: route -> a queue of responses (the last one repeats)

type Reply = { status?: number; body?: unknown; network?: boolean };
let routes: Record<string, Reply[]>;
let calls: { url: string; init?: RequestInit }[];

const ok = (body: unknown): Reply => ({ body });
const fail = (status: number, code: string, message: string): Reply => ({ status, body: { error: { code, message, request_id: "req-123" } } });
const down: Reply = { network: true };

function serve(table: Record<string, Reply | Reply[]>) {
  routes = Object.fromEntries(Object.entries(table).map(([k, v]) => [k, Array.isArray(v) ? [...v] : [v]]));
}
const happy = (e: Experiment = experiment()) => ({
  "/v1/datasets": ok([{ name: "synthetic", outcome: "visit", units: 39684, quarantined: 1300, quarantine_reasons: {}, experiment_id: "exp-1", trained_at: null, stale: false }]),
  "/v1/datasets/synthetic/experiment": ok(e),
  "/v1/runs/run-x_learner/deciles": ok(deciles),
  "/v1/runs/run-x_learner/profiles": ok({ high_uplift: group(0.15, "helps"), negative_uplift: group(-0.05, "hurts") }),
  "/v1/experiments/exp-1/segments": ok({ items: [segment("history = $350 and over AND channel = Phone", -0.06, "hurts")], next_cursor: null }),
});

beforeEach(() => {
  calls = [];
  tuning.retryDelays = [0, 0];
  tuning.freshFor = 60_000;
  sessionStorage.setItem("uplift-token", "read-token");
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const queue = routes[url.split("?")[0]];
    if (!queue) throw new Error(`unexpected request ${url}`);
    const reply = queue.length > 1 ? queue.shift()! : queue[0];
    if (reply.network) throw new TypeError("Failed to fetch");
    return new Response(reply.body instanceof Blob ? reply.body : JSON.stringify(reply.body), { status: reply.status ?? 200 });
  });
});

const count = (path: string) => calls.filter((c) => c.url.split("?")[0] === path).length;

describe("signing in", () => {
  it("asks for a token, sends it as a bearer header, and returns to the form when it is rejected", async () => {
    sessionStorage.clear();
    serve({ "/v1/datasets": fail(401, "unauthorized", "no") });
    const user = userEvent.setup();
    render(<App />);
    const input = screen.getByLabelText("Access token");
    expect(input).toHaveFocus();
    await user.type(input, "wrong-token{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("That token was not accepted");
    expect((calls[0].init!.headers as Record<string, string>).Authorization).toBe("Bearer wrong-token");
    expect(sessionStorage.getItem("uplift-token")).toBeNull();
  });
});

describe("comparing models", () => {
  it("shows real progress while loading, then the comparison with the champion named", async () => {
    serve(happy());
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading datasets");
    expect(screen.getByRole("progressbar", { name: "Loading datasets" })).toBeInTheDocument();
    expect(await screen.findByText(/ranks customers best on held-out data/)).toHaveTextContent("X-learner");
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("X-learner ★"); // sorted by Qini, champion marked with a symbol
    expect(within(table).getByText("Likely buyers (baseline)")).toBeInTheDocument();
    expect(table).toHaveTextContent("Qini ×100");
    const headers = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers.join(" ")).not.toMatch(/accuracy|AUC|F1|precision|recall/i); // uplift metrics only
    expect(screen.getByText(/0.5 means no leakage/)).toHaveTextContent("ratio test p = 0.61");
  });

  it("reads the chart out by keyboard: arrow keys move along the share targeted", async () => {
    serve(happy());
    const user = userEvent.setup();
    render(<App />);
    const chart = await screen.findByRole("img", { name: /Qini curve for 4 models/ });
    expect(screen.getByText(/Targeting the top/)).toHaveTextContent("30%");
    chart.focus();
    await user.keyboard("{ArrowRight}{ArrowRight}{PageUp}");
    expect(screen.getByText(/Targeting the top/)).toHaveTextContent("42%");
    await user.keyboard("{Home}");
    expect(screen.getByText(/Targeting the top/)).toHaveTextContent("0%");
    // the readout ranks the models at that point and names each one in text, not by colour
    await user.keyboard("{End}{ArrowLeft}");
    const readout = screen.getByText(/Targeting the top/).parentElement!;
    expect(within(readout).getAllByRole("listitem")[0]).toHaveTextContent("X-learner");
    await user.click(screen.getByRole("radio", { name: "Cumulative uplift" }));
    expect(screen.getByRole("img", { name: /Cumulative uplift curve/ })).toBeInTheDocument();
  });

  it("says so when the system has no experiment yet (empty)", async () => {
    serve({ "/v1/datasets": ok([]) });
    render(<App />);
    expect(await screen.findByText("No trained experiment yet.")).toBeInTheDocument();
  });

  it("explains an empty dataset from the server's own message", async () => {
    serve({ ...happy(), "/v1/datasets/synthetic/experiment": fail(404, "no_experiment", "Dataset 'synthetic' has no completed experiment yet.") });
    render(<App />);
    expect(await screen.findByText("Nothing here yet.")).toBeInTheDocument();
    expect(screen.getByText(/has no completed experiment yet/)).toBeInTheDocument();
  });

  it("retries transient failures on its own, then hands the error to the user with a way out", async () => {
    serve({ ...happy(), "/v1/datasets/synthetic/experiment": [fail(503, "database_unavailable", "The database cannot be reached right now. Retry shortly.")] });
    const user = userEvent.setup();
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the experiment. The database cannot be reached right now.");
    expect(alert).toHaveTextContent("Reference: req-123");
    expect(count("/v1/datasets/synthetic/experiment")).toBe(3); // 1 try + 2 retries, then stop
    serve(happy());
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/ranks customers best/)).toBeInTheDocument();
  });

  it("does not retry an error that retrying cannot fix", async () => {
    serve({ ...happy(), "/v1/datasets/synthetic/experiment": fail(400, "bad", "Bad request.") });
    render(<App />);
    await screen.findByRole("alert");
    expect(count("/v1/datasets/synthetic/experiment")).toBe(1);
  });

  it("recovers by itself when the first attempt fails and the retry succeeds", async () => {
    serve({ ...happy(), "/v1/datasets/synthetic/experiment": [down, ok(experiment())] });
    render(<App />);
    expect(await screen.findByText(/ranks customers best/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows partial results when a model failed to train, and says which", async () => {
    const failed = run("causal_forest", 0.5, { status: "failed", error: "MemoryError: out of memory", metrics: null, curves: null });
    serve(happy(experiment({ runs: [...experiment().runs, failed] })));
    render(<App />);
    const banner = await screen.findByText("Partial results.");
    expect(banner.parentElement).toHaveTextContent("1 of 5 models failed to train");
    expect(banner.parentElement).toHaveTextContent("Causal forest (MemoryError: out of memory)");
    expect(screen.getByRole("img", { name: /Qini curve for 4 models/ })).toBeInTheDocument(); // the rest still render
  });

  it("flags models that are older than the data", async () => {
    serve(happy(experiment({ stale: true })));
    render(<App />);
    expect(await screen.findByText("These models are out of date.")).toBeInTheDocument();
  });

  it("caches: switching views and back makes no new requests", async () => {
    serve(happy());
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/ranks customers best/);
    await user.click(screen.getByRole("button", { name: "Who it helps and hurts" }));
    await screen.findByText("Predicted to be harmed");
    await user.click(screen.getByRole("button", { name: "Compare models" }));
    await user.click(screen.getByRole("button", { name: "Who it helps and hurts" }));
    await screen.findByText("Predicted to be harmed");
    expect(count("/v1/datasets/synthetic/experiment")).toBe(1);
    expect(count("/v1/runs/run-x_learner/deciles")).toBe(1);
  });

  it("keeps showing saved results, marked stale, when a refresh fails", async () => {
    serve(happy());
    const first = render(<App />);
    await screen.findByText(/ranks customers best/);
    first.unmount();
    tuning.freshFor = 0; // the saved copy is now old enough to revalidate
    serve({ ...happy(), "/v1/datasets/synthetic/experiment": down });
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByText(/Showing saved results from/)).toBeInTheDocument();
    expect(screen.getByText(/ranks customers best/)).toBeInTheDocument(); // the data is still on screen
    serve(happy());
    await user.click(screen.getByRole("button", { name: "Retry now" }));
    await waitFor(() => expect(screen.queryByText(/Showing saved results from/)).not.toBeInTheDocument());
  });
});

describe("who it helps and hurts", () => {
  it("moves focus to the new view, and names harm in words", async () => {
    serve(happy());
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/ranks customers best/);
    await user.click(screen.getByRole("button", { name: "Who it helps and hurts" }));
    expect(screen.getByRole("heading", { level: 2, name: "Who it helps and hurts" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "Who it helps and hurts" })).toHaveAttribute("aria-current", "page");
    const harmed = (await screen.findByText("Predicted to be harmed")).parentElement!;
    expect(harmed).toHaveTextContent("−5.0 pts");
    expect(harmed).toHaveTextContent("▼ hurts");
    expect(harmed).toHaveTextContent("channel = Phone: 90% of the group vs 44% overall");
    const row = (await screen.findByText("history = $350 and over AND channel = Phone")).closest("tr")!;
    expect(row).toHaveTextContent("▼ hurts");
    expect(row).toHaveTextContent("<0.001");
    // the decile chart has a table equivalent
    await user.click(screen.getByText("Decile table"));
    expect(screen.getAllByRole("table")[0]).toHaveTextContent("+14.0 pts");
  });

  it("treats 'no segment is harmed' as a finding, and pages through the rest", async () => {
    const page1 = { items: [segment("newbie = yes", 0.09, "helps")], next_cursor: "abc" };
    const page2 = { items: [segment("channel = Web", 0.08, "helps")], next_cursor: null };
    serve({ ...happy(), "/v1/experiments/exp-1/segments": [ok({ items: [], next_cursor: null }), ok(page1), ok(page2)] });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/ranks customers best/);
    await user.click(screen.getByRole("button", { name: "Who it helps and hurts" }));
    expect(await screen.findByText(/no segment with statistically reliable harm/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Show"), "helps");
    expect(await screen.findByText("newbie = yes")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show more segments" }));
    expect(await screen.findByText("channel = Web")).toBeInTheDocument();
    expect(screen.getByText("newbie = yes")).toBeInTheDocument();
    expect(calls.at(-1)!.url).toContain("cursor=abc");
    expect(screen.queryByRole("button", { name: "Show more segments" })).not.toBeInTheDocument();
  });
});

describe("building a policy", () => {
  const open = async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/ranks customers best/);
    await user.click(screen.getByRole("button", { name: "Targeting policy" }));
    return user;
  };

  it("creates the policy, focuses the result, and compares it with both blanket policies", async () => {
    serve({ ...happy(), "/v1/policies": ok(policy), "/v1/policies/pol-1/export": { body: new Blob(["unit_id\nC1\n"]) } });
    URL.createObjectURL = vi.fn(() => "blob:x");
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const user = await open();
    await user.clear(screen.getByLabelText(/^Budget/));
    await user.type(screen.getByLabelText(/^Budget/), "1500");
    await user.click(screen.getByRole("button", { name: "Build policy" }));
    const heading = await screen.findByRole("heading", { name: "Treat 1,500 of 8,000 customers" });
    expect(heading).toHaveFocus();
    const result = heading.parentElement!;
    expect(result).toHaveTextContent("This policy3,610.00");
    expect(result).toHaveTextContent("Treat everyone3,200.00");
    expect(result).toHaveTextContent("Treat no one3,000.00");
    expect(result).toHaveTextContent("Versus treating everyone: +410.00 per 1,000 customers");
    expect(result).toHaveTextContent("▲ reliably better");
    expect(result).toHaveTextContent("Treats 19% of customers and captures 46% of the effect");
    const post = calls.find((c) => c.init?.method === "POST")!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ run_id: "run-x_learner", budget: 1500, cost_per_treatment: 1, value_per_outcome: 20 });
    await user.click(screen.getByRole("button", { name: "Download activation file (CSV)" }));
    expect(await screen.findByText("Downloaded 1,500 customer ids.")).toBeInTheDocument();
    expect(click).toHaveBeenCalledOnce();
  });

  it("is safe to retry: the same request reuses its idempotency key, a changed one gets a new key", async () => {
    serve({ ...happy(), "/v1/policies": [down, ok(policy), ok(policy)] });
    const user = await open();
    await user.click(screen.getByRole("button", { name: "Build policy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not build the policy. The server cannot be reached.");
    await user.click(screen.getByRole("button", { name: "Build policy" }));
    await screen.findByRole("heading", { name: /Treat 1,500/ });
    await user.type(screen.getByLabelText(/^Budget/), "0");
    await user.click(screen.getByRole("button", { name: "Build policy" }));
    await waitFor(() => expect(calls.filter((c) => c.init?.method === "POST")).toHaveLength(3));
    const keys = calls.filter((c) => c.init?.method === "POST").map((c) => (c.init!.headers as Record<string, string>)["Idempotency-Key"]);
    expect(keys[0]).toBe(keys[1]);
    expect(keys[2]).not.toBe(keys[0]);
  });

  it("tells a read-only user what they need instead of failing vaguely", async () => {
    serve({ ...happy(), "/v1/policies": fail(403, "forbidden", "no") });
    const user = await open();
    await user.click(screen.getByRole("button", { name: "Build policy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign in with an admin token");
  });

  it("explains a policy too small to evaluate, and one that is waiting for batch scoring", async () => {
    const tiny: Policy = { ...policy, n_targeted: 5, spend: "5.0000", evaluation: { evaluable: false, reason: "fewer than 30 experiment units in a cell" } };
    serve({ ...happy(), "/v1/policies": ok(tiny) });
    const user = await open();
    await user.click(screen.getByRole("button", { name: "Build policy" }));
    expect(await screen.findByText("Too small to evaluate.")).toBeInTheDocument();
  });

  it("asks for batch scoring when the champion has no scores", async () => {
    const e = experiment();
    e.runs[0].scored = false;
    serve(happy(e));
    await open();
    expect(screen.getByText("The champion model has not been batch-scored yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Build policy" })).not.toBeInTheDocument();
  });
});
