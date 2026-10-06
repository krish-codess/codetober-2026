// Component tests for the interactive logic and for every state the page can be in.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { App } from "./App";
import type { Cell, Recommendation, Results, Variant } from "./api";

const QUERIES = [
  { id: "projection", class: "projection", sql: "SELECT 1", description: "Three columns." },
  { id: "filter_clustered", class: "filter_clustered", sql: "SELECT 2", description: "1% filter.", control: "projection" },
];
const cell = (ms: number, read: number, size: number): Cell => ({
  ok: true, cpu_s: ms / 1000, bytes_read: read, reads: 4, read_fraction: read / size,
  cold: { n: 3, median_s: ms / 500, min_s: ms / 600, max_s: ms / 400, stdev_s: 0.001, cv: 0.08 },
  warm: { n: 5, median_s: ms / 1000, min_s: ms / 1100, max_s: ms / 900, stdev_s: 0.001, cv: 0.04 },
});
const variant = (id: string, format: Variant["format"], bytes: number, ms: number, write_s: number): Variant => ({
  id, format, codec: id.split("-")[1], level: null, chunk: null, layout: "sorted", reader: "DuckDB native", bytes, rows: 1000,
  write_s, write_cpu_s: write_s, chunks: format === "parquet" ? 9 : null, ratio_vs_csv: 1e9 / bytes,
  queries: { projection: cell(ms, bytes / 5, bytes), filter_clustered: cell(ms / 2, bytes / 50, bytes) },
});
const results = (): Results => ({
  generated_at: new Date().toISOString().slice(0, 19) + "Z", origin: "published", partial: false, missing: [],
  dataset: { name: "nyc_yellow_taxi", rows: 9_554_426, csv_bytes: 1e9, landed: 9_554_778, columns: 20, sort_key: "pickup_datetime",
    source: "tlc", months: ["2024-01"], quarantined: { duplicate: 2, unparseable: 350 }, warnings: {} },
  env: { cpu: "Test CPU", logical_cpus: 8, ram_gb: 16, os: "TestOS", versions: { duckdb: "1.5.6" },
    eviction: { supported: true, effective: false, cached_mb_s: 900, evicted_mb_s: 800 },
    settings: { cold_runs: 3, warmups: 1, warm_runs: 5, cell_budget_s: 20 } },
  queries: QUERIES,
  variants: [variant("csv-none", "csv", 1e9, 4000, 20), variant("parquet-zstd", "parquet", 1.5e8, 40, 9),
    variant("orc-zstd", "orc", 1.4e8, 90, 12), variant("avro-snappy", "avro", 3.5e8, 9000, 30)],
  pushdown: [{ variant: "parquet-zstd", format: "parquet", reader: "DuckDB native", read_fraction: { projection: 0.2, filter_clustered: 0.02 },
    pruned_vs_control: { filter_clustered: 0.9 }, projection_pushdown: true, predicate_pushdown: true },
  { variant: "csv-none", format: "csv", reader: "DuckDB native", read_fraction: { projection: 1, filter_clustered: 1 },
    pruned_vs_control: { filter_clustered: 0 }, projection_pushdown: false, predicate_pushdown: false }],
  columns: { lab: { rows: 1000, columns: [{ column: "int_random", type: "int64", distinct: 1000, null_frac: 0, raw_bytes: 8000 }],
    cells: [{ column: "int_random", variant: "parquet-zstd", format: "parquet", codec: "zstd", bytes: 8100, ratio: 0.99 }] } },
  stages: {},
});
const WHY = "parquet-zstd costs $12.34/month.";
const recommendation: Recommendation = {
  pick: "parquet-zstd", why: ["parquet-zstd costs $12.34/month."], excluded: [], assumptions: ["Linear extrapolation."],
  price: { label: "AWS" }, workload: { mix: { projection: 1 } },
  ranked: [{ variant: "parquet-zstd", format: "parquet", codec: "zstd", storage: 2, scan: 10, compute: 0, requests: 0.3, write: 0.04, total: 12.34, latency_s: 0.04, score: 2 },
    { variant: "csv-none", format: "csv", codec: "none", storage: 11.5, scan: 100, compute: 0, requests: 0.1, write: 0.02, total: 111.62, latency_s: 4, score: 100 }],
};

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(status === 304 ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const apiError = (status: number, code: string, message: string, details: unknown[] = []) =>
  json({ error: { code, message, details, request_id: "t" } }, status);

type Handler = (init?: RequestInit) => Response | Promise<Response>;
function mockApi(routes: { results?: Handler; recommend?: Handler }) {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const handler = String(url).includes("/api/recommend") ? routes.recommend ?? (() => json(recommendation)) : routes.results ?? (() => json(results(), 200, { ETag: '"v1"' }));
    return handler(init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
const posts = (m: ReturnType<typeof mockApi>) => m.mock.calls.filter(([url]) => String(url).includes("recommend"));

test("loading, then the run summary, chart and tables", async () => {
  let release!: (r: Response) => void;
  mockApi({ results: () => new Promise((resolve) => { release = resolve; }) });
  render(<App />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading results");
  expect(screen.getByRole("progressbar", { name: "Results downloaded" })).toBeInTheDocument();
  release(json(results(), 200, { ETag: '"v1"' }));
  expect(await screen.findByText("9,554,426")).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: /warm median/ })).toHaveLength(4);
  expect(screen.getByText(/page cache eviction NOT verified/)).toBeInTheDocument(); // honesty about cold runs is part of the UI
});

test("empty: no benchmark yet explains what to run", async () => {
  mockApi({ results: () => apiError(404, "no_results", "No benchmark results yet. Run `bakeoff all`, then reload.") });
  render(<App />);
  expect(await screen.findByRole("heading", { name: "No benchmark has been run yet" })).toBeInTheDocument();
  expect(screen.getByText(/bakeoff all/)).toBeInTheDocument();
});

test("error: shows the server's message, focuses Retry, and recovers", async () => {
  let fail = true;
  mockApi({ results: () => (fail ? apiError(503, "results_unreadable", "results.json cannot be used.") : json(results())) });
  render(<App />);
  expect(await screen.findByRole("alert")).toHaveTextContent("results.json cannot be used.");
  const retry = screen.getByRole("button", { name: "Retry" });
  expect(retry).toHaveFocus();
  fail = false;
  await userEvent.click(retry);
  expect(await screen.findByText("9,554,426")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("stale: a failed refresh keeps the cached copy on screen and says so", async () => {
  mockApi({});
  const first = render(<App />);
  await screen.findByText("9,554,426");
  first.unmount();
  mockApi({ results: () => Promise.reject(new TypeError("Failed to fetch")) });
  render(<App />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Showing a saved copy");
  expect(screen.getByText("9,554,426")).toBeInTheDocument();
});

test("cache: revalidates with the ETag and accepts a 304 without a body", async () => {
  mockApi({});
  render(<App />).unmount();
  await waitFor(() => expect(localStorage.getItem("bakeoff.results.v1")).toContain("v1"));
  const m = mockApi({ results: () => json(null, 304) });
  render(<App />);
  expect(await screen.findByText("9,554,426")).toBeInTheDocument();
  await waitFor(() => expect((m.mock.calls[0][1]?.headers as Record<string, string>)["If-None-Match"]).toBe('"v1"'));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("partial: failed measurements are flagged in the banner, the table and the chart", async () => {
  const r = results();
  r.partial = true;
  r.missing = ["orc-zstd/projection"];
  r.variants[2].queries.projection = { ok: false, error: "IOException: truncated file" };
  mockApi({ results: () => json(r) });
  render(<App />);
  expect(await screen.findByText(/Partial results/)).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: /warm median/ })).toHaveLength(3);
  expect(screen.getByText(/1 variant not plotted/)).toBeInTheDocument();
  const row = within(screen.getByRole("region", { name: "Every variant, sortable" })).getByRole("row", { name: /orc-zstd/ });
  expect(within(row).getByText(/failed/)).toHaveAttribute("title", "IOException: truncated file");
});

test("chart: arrow keys move between marks, Enter selects, the legend hides a format", async () => {
  mockApi({});
  const user = userEvent.setup();
  render(<App />);
  const marks = await screen.findAllByRole("button", { name: /warm median/ });
  expect(marks.filter((m) => m.getAttribute("tabindex") === "0")).toHaveLength(1); // one tab stop for the whole chart
  marks[0].focus();
  await user.keyboard("{ArrowRight}");
  expect(marks[1]).toHaveFocus();
  expect(screen.getByRole("presentation")).toHaveTextContent(/File size/); // tooltip follows keyboard focus too
  await user.keyboard("{Enter}");
  expect(marks[1]).toHaveAttribute("aria-pressed", "true");
  const picked = marks[1].getAttribute("aria-label")!.split(":")[0];
  expect(screen.getByRole("heading", { level: 3, name: picked })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: `Measurements for ${picked}` })).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Avro" }));
  expect(screen.getByRole("button", { name: "Avro" })).toHaveAttribute("aria-pressed", "false");
  expect(screen.getAllByRole("button", { name: /warm median/ })).toHaveLength(3);
});

test("table: sorting is announced, and selecting a row moves focus to its detail", async () => {
  mockApi({});
  const user = userEvent.setup();
  render(<App />);
  const table = within(await screen.findByRole("region", { name: "Every variant, sortable" }));
  const ids = () => table.getAllByRole("rowheader").map((h) => h.textContent);
  expect(ids()).toEqual(["orc-zstd", "parquet-zstd", "avro-snappy", "csv-none"]); // by size
  await user.click(table.getByRole("button", { name: /^projection/ }));
  expect(ids()[0]).toBe("parquet-zstd");
  expect(table.getByRole("columnheader", { name: /^projection/ })).toHaveAttribute("aria-sort", "ascending");
  await user.click(table.getByRole("button", { name: "csv-none" }));
  await waitFor(() => expect(screen.getByRole("heading", { level: 3, name: "csv-none" })).toHaveFocus());
});

test("recommendation: asks the API once per distinct workload and shows the pick", async () => {
  const m = mockApi({});
  const user = userEvent.setup();
  render(<App />);
  expect(await screen.findByText(WHY)).toBeInTheDocument();
  expect(screen.getAllByText("$12.34")).toHaveLength(2); // headline and table
  expect(JSON.parse(String(posts(m)[0][1]?.body))).toMatchObject({ dataset_gb: 500, provider: "aws", mix: { projection: 3 } });

  await user.selectOptions(screen.getByLabelText("Cloud"), "gcp");
  await waitFor(() => expect(posts(m)).toHaveLength(2));
  await user.selectOptions(screen.getByLabelText("Cloud"), "aws"); // back to a workload already priced
  await new Promise((r) => setTimeout(r, 400));
  expect(posts(m)).toHaveLength(2);
});

test("recommendation: invalid input is explained inline and never sent", async () => {
  const m = mockApi({});
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText(WHY);
  const size = screen.getByLabelText(/Dataset size/);
  await user.clear(size);
  expect(size).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByText("Enter a size above 0 GB.")).toBeInTheDocument();
  expect(screen.getByText(/Fix the highlighted field/)).toBeInTheDocument();
  await new Promise((r) => setTimeout(r, 400));
  expect(posts(m)).toHaveLength(1);
});

test("recommendation: a server error keeps the last answer visible and offers a retry", async () => {
  let broken = false;
  const m = mockApi({ recommend: () => (broken ? apiError(500, "internal", "Unexpected server error.") : json(recommendation)) });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText(WHY);
  broken = true;
  await user.selectOptions(screen.getByLabelText("Optimise for"), "cost");
  expect(await screen.findByText(/Could not price this workload: Unexpected server error./)).toBeInTheDocument();
  expect(screen.getByText(WHY)).toBeInTheDocument();
  broken = false;
  await user.click(screen.getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(screen.queryByText(/Could not price/)).not.toBeInTheDocument());
  expect(posts(m).length).toBeGreaterThanOrEqual(3);
});
