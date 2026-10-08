import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { type Bench, type Point, clearCache } from "./api";

const bench = (p50: number, energy: number | null): Bench => ({
  runs: 2, p50_ms: p50, p95_ms: p50 * 1.2, p99_ms: p50 * 1.5, top1_device: 0.97, agree_host: 1, load_w: energy === null ? null : 5.1,
  idle_w: energy === null ? null : 2.7, energy_mj: energy, power_source: energy === null ? null : "pmic",
  power_unavailable: energy === null ? "on AC power: unplug and re-run" : null, cpu_model: "Cortex-A76", threads: 4, last_measured_at: "2026-10-08T12:00:00+00:00",
}); // prettier-ignore
const point = (name: string, over: Partial<Point>): Point => ({
  name, parent: null, technique: "none", arch: "resnet50", precision: "fp32", params: 23_500_000, size_bytes: 94_000_000, top1: 0.982,
  top1_lo: 0.966, top1_hi: 0.991, n_eval: 500, top1_deploy: 0.958, parent_agree: null, parent_delta: null, parent_delta_lo: null,
  parent_delta_hi: null, gate: "pass", gate_reason: "top-1 0.982 vs floor 0.962", bench: null, pareto: false, meets_budget: null, ...over,
}); // prettier-ignore

const target = { name: "rpi5", label: "Raspberry Pi 5", arch: "aarch64", runtimes: ["onnxruntime"], threads: 4, budget_p95_ms: 50, power_sensor: "pmic", results: 6, packages: 1 };
const spare = { ...target, name: "rpi4", label: "Raspberry Pi 4", results: 0, packages: 0 };
const points = (energy: number | null = 48) => [
  point("teacher-r50", { bench: bench(400, energy), pareto: true, meets_budget: false }),
  point("student-kd", { parent: "teacher-r50", technique: "distillation", arch: "mobilenet_v3_small", top1: 0.97, top1_lo: 0.95, top1_hi: 0.98, parent_agree: 0.97, parent_delta: -0.012, parent_delta_lo: -0.03, parent_delta_hi: 0.004, bench: bench(20, energy), pareto: true, meets_budget: true }),
  point("student-kd-int8", { parent: "student-kd", technique: "quantization", arch: "mobilenet_v3_small", precision: "int8", top1: 0.15, top1_lo: 0.12, top1_hi: 0.18, gate: "fail", gate_reason: "top-1 0.150 vs floor 0.962", bench: bench(9, energy) }),
  point("r50-prune50", { parent: "teacher-r50", technique: "pruning+finetune", arch: "resnet50-pruned50", top1: 0.97 }),
];
const tradeoff = (pts = points()) => ({
  target, runtime: "onnxruntime", runtimes_measured: ["onnxruntime"],
  run: { run_id: "abc123", created_at: "2026-10-08T10:00:00Z", git_commit: "deadbeef", dataset_version: "d1", teacher_top1: 0.982, budget_pt: 2 },
  baselines: [{ name: "majority class", top1: 0.1 }], points: pts,
}); // prettier-ignore
const sensitivity = { run_id: "abc123", variant: "student-kd", rows: [
  { rank: 1, node: "/body/features/features.0/features.0.0/Conv", op_type: "Conv", kl: 3.7, top1_drop: 0.86, kept_float: true },
  { rank: 2, node: "/body/features/features.2/block/block.0/block.0.0/Conv", op_type: "Conv", kl: 0.004, top1_drop: 0, kept_float: false },
] }; // prettier-ignore
const packages = [
  { package_id: "a".repeat(64), name: "squeeze-rpi5-abc123", target: "rpi5", run_id: "abc123", filename: "squeeze-rpi5-abc123.tar.gz", size_bytes: 5e6, git_commit: "deadbeef", dataset_version: "d1", hosted: true, variants: [{ name: "student-kd", precision: "fp32", size_bytes: 6e6, top1_host: 0.97 }] },
  { package_id: "b".repeat(64), name: "squeeze-rpi4-abc123", target: "rpi4", run_id: "abc123", filename: "squeeze-rpi4-abc123.tar.gz", size_bytes: 2e8, git_commit: "deadbeef", dataset_version: "d1", hosted: false, variants: [] },
]; // prettier-ignore

type Route = (url: string, init?: RequestInit) => Response | Promise<Response>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { etag: '"v1"' } });
function serve(over: Record<string, Route> = {}) {
  const routes: Record<string, Route> = {
    "/v1/targets": () => json([target, spare]),
    "/v1/tradeoff": () => json(tradeoff()),
    "/v1/sensitivity": () => json(sensitivity),
    "/v1/packages": () => json(packages),
    ...over,
  };
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    return Promise.resolve(routes[url.split("?")[0]](url, init));
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

beforeEach(() => {
  clearCache();
  localStorage.clear();
  location.hash = "";
});

test("opens on the most-measured target with chart, table, and what is still missing", async () => {
  serve();
  render(<App />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading hardware targets: 0 of 1 requests done");
  expect(await screen.findByRole("heading", { name: "Accuracy against cost on Raspberry Pi 5" })).toBeVisible();
  // failed variants are off the chart by default; the unmeasured one cannot be on it
  expect(screen.getAllByRole("button", { name: /top-1, .* ms/ }).map((m) => m.getAttribute("aria-label")!.split(":")[0])).toEqual(["teacher-r50", "student-kd"]);
  const row = (name: string) => screen.getByRole("row", { name: new RegExp(`^${name}\\b`) });
  expect(row("r50-prune50")).toHaveTextContent("not measured on this target");
  expect(row("student-kd-int8")).toHaveTextContent("✕ fail");
  expect(row("teacher-r50")).toHaveTextContent("⚠ over");
  expect(screen.getByText(/1 variant is not measured on this target yet/)).toBeVisible();
  expect(screen.getByText(/most accurate within budget:/)).toHaveTextContent("student-kd");
  expect(screen.getByText("kept float32")).toBeVisible();
});

test("failed variants join the chart only on request, and say so without relying on colour", async () => {
  serve();
  render(<App />);
  await userEvent.click(await screen.findByRole("checkbox", { name: "Show variants that failed the gate" }));
  expect(screen.getByRole("button", { name: /^student-kd-int8: .*INT8, failed the accuracy gate/ })).toBeVisible();
});

test("a mark is operable from the keyboard and focus moves to the detail it opens", async () => {
  serve();
  render(<App />);
  const mark = await screen.findByRole("button", { name: /^student-kd:/ });
  mark.focus();
  expect(await screen.findByText(/p50 20.0 ms, p95 24.0 ms/)).toBeVisible(); // tooltip on focus, not only hover
  await userEvent.keyboard("{Enter}");
  const heading = await screen.findByRole("heading", { name: "student-kd" });
  await waitFor(() => expect(heading).toHaveFocus());
  const detail = heading.closest("section")!;
  expect(within(detail).getByText(/✓ Passed the accuracy gate/)).toBeVisible();
  expect(within(detail).getAllByRole("listitem").map((li) => li.textContent!.split(" ")[0])).toEqual(["teacher-r50", "student-kd"]);
  expect(detail).toHaveTextContent("−1.2 pt vs parent (-3.0 to 0.4)");
  expect(detail).toHaveTextContent("48.0 mJ per inference net of idle");
});

test("no power sensor: the energy axis explains why instead of drawing an empty chart", async () => {
  serve({ "/v1/tradeoff": () => json(tradeoff(points(null))) });
  render(<App />);
  await userEvent.click(await screen.findByRole("radio", { name: "Energy per inference" }));
  expect(screen.getByText(/Power was not measured on this target: on AC power/)).toBeVisible();
  expect(screen.getByRole("row", { name: /^student-kd(?!-)/ })).toHaveTextContent("no sensor");
});

test("a target nothing was measured on still shows verified accuracy", async () => {
  serve({ "/v1/tradeoff": () => json(tradeoff(points().map((p) => ({ ...p, bench: null, pareto: false })))) });
  render(<App />);
  expect(await screen.findByText(/Nothing has been measured on Raspberry Pi 5 with onnxruntime yet/)).toBeVisible();
  expect(screen.getAllByRole("row")).toHaveLength(5);
});

test("empty system: says how to produce the first run", async () => {
  serve({ "/v1/tradeoff": () => json({ ...tradeoff([]), run: null }), "/v1/sensitivity": () => json({ error: { message: "no pipeline run has been ingested yet" }, request_id: "r1" }, 404) });
  render(<App />);
  expect(await screen.findByText("No pipeline run has been ingested yet.")).toBeVisible();
});

test("error with nothing cached: says what failed, with the request id, and retries", async () => {
  let fail = true;
  serve({ "/v1/tradeoff": () => (fail ? json({ error: { code: "internal", message: "unexpected error; quote the request_id" }, request_id: "req-7" }, 500) : json(tradeoff())) });
  render(<App />);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Could not load the tradeoff. unexpected error; quote the request_id (request req-7)");
  fail = false;
  await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));
  expect(await screen.findByRole("button", { name: /^student-kd:/ })).toBeVisible();
});

test("server down after a good load: keeps the data, marks it stale, and revalidates with the etag", async () => {
  const mock = serve();
  const first = render(<App />);
  await screen.findByRole("button", { name: /^student-kd:/ });
  first.unmount();
  clearCache(); // a new page load: only localStorage survives

  mock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/v1/tradeoff")) return Promise.reject(new TypeError("Failed to fetch"));
    expect((init?.headers as Record<string, string>)["if-none-match"]).toBe('"v1"');
    return Promise.resolve(new Response(null, { status: 304 }));
  });
  render(<App />);
  const banner = await screen.findByText(/Showing saved data from/);
  expect(banner.parentElement).toHaveTextContent("The server could not be reached (Failed to fetch)");
  expect(screen.getByRole("button", { name: /^student-kd:/ })).toBeVisible();
  expect(screen.getByRole("row", { name: /^teacher-r50\b/ })).toBeVisible();
});

test("package browser: download what is hosted, rebuild what is not, and say when there is none", async () => {
  serve({ "/v1/targets": () => json([target, spare, { ...spare, name: "ci", label: "CI runner" }]) });
  location.hash = "view=packages";
  render(<App />);
  const pi5 = (await screen.findByRole("heading", { name: "Raspberry Pi 5" })).closest("section")!;
  expect(within(pi5).getByRole("link", { name: "Download package" })).toHaveAttribute("href", `/v1/packages/${"a".repeat(64)}/download`);
  expect(within(pi5).getByText(/student-kd/)).toHaveTextContent("97.0% top-1");
  const pi4 = screen.getByRole("heading", { name: "Raspberry Pi 4" }).closest("section")!;
  expect(within(pi4).queryByRole("link", { name: "Download package" })).toBeNull();
  expect(pi4).toHaveTextContent("python -m squeeze package --target rpi4");
  expect(screen.getByRole("heading", { name: "CI runner" }).closest("section")).toHaveTextContent("No package has been built for this target.");
});
