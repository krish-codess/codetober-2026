// Every state the page can be in: loading, error, empty, ready, partial data, stale data, and the
// degraded modes. The WebGL scene and DuckDB are replaced by fakes; their real behaviour is
// covered by lib/*.test.ts and by the end-to-end test.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import type { MetaDb } from "./lib/db";
import type { SceneLike, SceneOptions, SceneStatus, SelectionInfo } from "./lib/scene";
import type { Manifest } from "./lib/types";

const manifest: Manifest = {
  format: 1,
  build_id: "build-a",
  created_at: "2026-10-09T10:00:00Z",
  source: { name: "glove-twitter-50", synthetic: false, url: "https://example.org/x.zip", lines: 1003, accepted: 1000, quarantined: 3, quarantine_reasons: { duplicate_token: 3 } },
  n_points: 1000,
  tile_capacity: 256,
  columns: [
    { name: "script", labels: ["(none)", "Latin", "Arabic"], counts: [50, 700, 250] },
    { name: "kind", labels: ["(none)", "word", "hashtag"], counts: [0, 900, 100] },
    { name: "cluster", labels: ["(none)", "c01: the a", "c02: في من"], counts: [0, 600, 400] },
  ],
  meta: { path: "build-a/meta.parquet", bytes: 1000, rows: 1000 },
  eval: { k: 15, chance: { recall: 0.001, purity: { script: 0.5, kind: 0.8, cluster: 0.5 } } },
  variants: ["pca", "umap-n15-d0.1"].map((name) => ({
    name,
    label: name === "pca" ? "PCA (linear baseline)" : "UMAP · 15 neighbours · min_dist 0.1",
    params: {},
    tiles_path: `build-a/${name}`,
    bounds: [0, 0, 1],
    view: [0, 0, 1, 1],
    tiles: [[0, 0, 0, 256]],
    n_points: 1000,
    dropped_non_finite: 0,
    density_grid: 256,
    bytes: 12000,
    metrics: name === "pca" ? null : { recall: { all: { mean: 0.31, lo: 0.3, hi: 0.32, n: 400 }, fitted: null, placed: { mean: 0.29, lo: 0.28, hi: 0.3, n: 200 } }, purity: { script: { mean: 0.9, lo: 0.89, hi: 0.91, n: 400 } }, recall_vs_pca: { mean: 0.2, lo: 0.19, hi: 0.21, n: 400 } },
  })),
};

const status: SceneStatus = { tilesWanted: 4, tilesLoaded: 4, failed: [], pointsDrawn: 1000, pointsInView: 1000, pointsHeld: 1000, bytesLoaded: 12000, fps: 60, shading: "float", gpu: "test", firstPaintMs: 40 };

function counts(script: number[], kind: number[], cluster: number[]): Uint32Array[] {
  return [script, kind, cluster].map((c) => {
    const a = new Uint32Array(256);
    a.set(c);
    return a;
  });
}
const selection: SelectionInfo = { n: 200, counts: counts([0, 20, 180], [0, 150, 50], [0, 0, 200]), ids: Uint32Array.of(1, 2, 3), live: false, exact: true, workerMs: 2.5, latencyMs: 9 };

/** A scene that records what it was asked to do and lets the test play the part of the GPU and worker. */
function fakeScene() {
  const calls: [string, ...unknown[]][] = [];
  let options!: SceneOptions;
  const scene = new Proxy({} as SceneLike, { get: (_, name: string) => (...args: unknown[]) => void calls.push([name, ...args]) });
  return { calls, create: (o: SceneOptions) => ((options = o), scene), get options() { return options; }, did: (name: string) => calls.filter((c) => c[0] === name) };
}

const fakeDb = (over: Partial<MetaDb> = {}): MetaDb => ({
  summary: async () => ({ rank: { min: 3, q25: 100, median: 420, q75: 900, max: 5000 }, meanChars: 5.5, top: [{ token: "في", rank: 3 }, { token: "من", rank: 9 }], ms: 12 }),
  lookup: async (id) => ({ id, token: "سلام", rank: 77, n_chars: 4, script: "Arabic", kind: "word", cluster: "c02" }),
  search: async () => [{ id: 5, token: "hello", rank: 40, n_chars: 5, script: "Latin", kind: "word", cluster: "c01" }],
  close: async () => undefined,
  ...over,
});

function serve(body: unknown, init: ResponseInit = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(typeof body === "string" ? body : JSON.stringify(body), init));
}

async function ready(openDb = async () => fakeDb(), m: Manifest = manifest) {
  serve(m);
  const scene = fakeScene();
  const view = render(<App dataUrl="http://data.test/" createScene={scene.create} openDb={openDb as never} pollMs={1e9} />);
  await screen.findByRole("application");
  await waitFor(() => expect(scene.options).toBeDefined()); // the element exists one tick before the effect that builds the scene
  act(() => scene.options.onStatus(status));
  return { scene, ...view };
}

test("loading, then the plot with its controls", async () => {
  let release!: (r: Response) => void;
  vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise((r) => (release = r)));
  const scene = fakeScene();
  render(<App dataUrl="http://data.test/" createScene={scene.create} openDb={(async () => fakeDb()) as never} />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading the dataset description");
  await act(async () => release(new Response(JSON.stringify(manifest))));
  expect(await screen.findByRole("application")).toHaveAccessibleName(/Scatter plot of 1,000 points.*Arrow keys pan/);
  await waitFor(() => expect(scene.options).toBeDefined());
  expect(screen.getByText(/3 rows quarantined at ingest/)).toBeInTheDocument();
  expect(screen.getByText(/Loading the first 256 points/)).toBeInTheDocument(); // nothing painted yet
  act(() => scene.options.onStatus(status));
  expect(screen.queryByText(/Loading the first/)).not.toBeInTheDocument();
  expect(scene.options.variant).toBe("umap-n15-d0.1");
  expect(scene.options.dataUrl).toBe("http://data.test/");
  expect(screen.getByText(/Drawing 1,000 of about 1,000 points in view/)).toBeInTheDocument();
  expect(screen.getByText(/Nothing selected/)).toBeInTheDocument();
});

test.each([
  [() => Promise.reject(new TypeError("offline")), /could not be reached/],
  [async () => new Response("", { status: 404 }), /No dataset has been published/],
  [async () => new Response("", { status: 502 }), /HTTP 502/],
  [async () => new Response("<html>"), /not valid JSON/],
  [async () => new Response(JSON.stringify({ ...manifest, format: 9 })), /format 9; this page reads format 1/],
  [async () => new Response(JSON.stringify({ hello: 1 })), /not in the expected shape/],
])("a manifest failure is explained and can be retried (%#)", async (respond, message) => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(respond as never);
  render(<App dataUrl="http://data.test/" createScene={fakeScene().create} />);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(message);
  const retry = within(alert).getByRole("button", { name: "Try again" });
  expect(retry).toHaveFocus();
  fetchMock.mockImplementation(async () => new Response(JSON.stringify(manifest)));
  await userEvent.click(retry);
  expect(await screen.findByRole("application")).toBeInTheDocument();
});

test("an empty dataset says so, and why", async () => {
  serve({ ...manifest, n_points: 0, source: { ...manifest.source, accepted: 0, quarantined: 1003 }, variants: manifest.variants.map((v) => ({ ...v, n_points: 0, tiles: [] })) });
  render(<App dataUrl="http://data.test/" createScene={fakeScene().create} />);
  expect(await screen.findByText("This dataset is empty")).toBeInTheDocument();
  expect(screen.getByText(/all 1,003 input rows were quarantined/)).toBeInTheDocument();
  expect(screen.queryByRole("application")).not.toBeInTheDocument();
});

test("synthetic data is labelled as synthetic", async () => {
  await ready(undefined, { ...manifest, source: { ...manifest.source, name: "synth-200000", synthetic: true } });
  expect(screen.getByText("synthetic data")).toBeInTheDocument();
});

test("a selection shows counts, purity, lift and timing; then token statistics from the local database", async () => {
  const { scene } = await ready();
  act(() => scene.options.onSelection({ ...selection, live: true, ids: null }));
  const headline = screen.getByText(/of 1,000 points/);
  expect(headline).toHaveTextContent("200 of 1,000 points (20.0%)");
  expect(headline).toHaveAttribute("role", "status");
  expect(screen.getByText("updating")).toBeInTheDocument();
  expect(screen.getByText("Release to list the tokens.")).toBeInTheDocument();
  const arabic = screen.getByRole("row", { name: /Arabic/ });
  expect(arabic).toHaveTextContent("180");
  expect(arabic).toHaveTextContent("90.0%");
  expect(arabic).toHaveTextContent("×3.6"); // 90% of the selection against 25% of the dataset
  expect(screen.getByText(/purity 90.0% · entropy 0.30/)).toBeInTheDocument();
  expect(screen.getByText(/Counted in 9 ms \(2.5 ms in the worker\), no server involved/)).toBeInTheDocument();

  act(() => scene.options.onSelection(selection));
  expect(await screen.findByText(/median 420/)).toBeInTheDocument();
  expect(screen.getByText("في")).toBeInTheDocument();
  expect(screen.getByText(/DuckDB-WASM, 12 ms/)).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Clear" }));
  expect(scene.did("clearSelection")).toHaveLength(1);
});

test("partial data: a selection over unloaded tiles is called a sample, and an empty one is not an error", async () => {
  const { scene } = await ready();
  act(() => scene.options.onSelection({ ...selection, exact: false }));
  expect(screen.getByRole("note")).toHaveTextContent(/Partial count.*counts a sample/);
  act(() => scene.options.onSelection({ ...selection, n: 0, counts: counts([], [], []), ids: new Uint32Array(0) }));
  expect(screen.getByText("No points inside the outline.")).toBeInTheDocument();
});

test("partial data: tiles that failed are reported with a retry, and loading shows real progress", async () => {
  const { scene } = await ready();
  act(() => scene.options.onStatus({ ...status, tilesWanted: 9, tilesLoaded: 5, failed: [] }));
  const bar = screen.getByRole("progressbar", { name: "tiles loaded" });
  expect(bar).toHaveAttribute("value", "5");
  expect(bar).toHaveAttribute("max", "9");
  act(() => scene.options.onStatus({ ...status, tilesWanted: 9, tilesLoaded: 7, failed: [{ key: "1-0-0", message: "HTTP 503" }, { key: "1-0-1", message: "HTTP 503" }] }));
  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent("2 tiles could not be loaded after several attempts (HTTP 503)");
  await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
  expect(scene.did("retryFailed")).toHaveLength(1);
});

test("without the token table the counts still work, and the table can be retried", async () => {
  let attempts = 0;
  const openDb = async () => {
    if (++attempts === 1) throw new Error("Could not download the metadata table: HTTP 503");
    return fakeDb();
  };
  const { scene } = await ready(openDb);
  // Before anything is selected, the search panel is where the failure is explained.
  expect(await screen.findByRole("alert")).toHaveTextContent(/token table could not be loaded.*HTTP 503.*selection counts still work/);
  act(() => scene.options.onSelection(selection));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/Token statistics are unavailable.*HTTP 503.*counts above do not depend on them/);
  expect(screen.getByText(/of 1,000 points/)).toHaveTextContent("200 of 1,000 points");
  expect(screen.getByRole("searchbox")).toBeDisabled();
  await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
  expect(await screen.findByText(/median 420/)).toBeInTheDocument();
});

test("the token table download shows its progress", async () => {
  let report!: (p: number) => void;
  const openDb = (_u: string, _b: number, onProgress: (p: number) => void) => ((report = onProgress), new Promise<MetaDb>(() => undefined));
  const { scene } = await ready(openDb as never);
  act(() => scene.options.onSelection(selection));
  act(() => report(0.43));
  expect(screen.getByRole("progressbar", { name: "token table download" })).toHaveAttribute("value", "0.43");
  expect(screen.getByText(/Available once the token table has loaded \(43%\)/)).toBeInTheDocument();
});

test("stale data: a newer build is offered, not swapped in", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const fetchMock = serve(manifest);
    const scene = fakeScene();
    render(<App dataUrl="http://data.test/" createScene={scene.create} openDb={(async () => fakeDb()) as never} pollMs={1000} />);
    await screen.findByRole("application");
    await waitFor(() => expect(scene.options).toBeDefined());
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ ...manifest, build_id: "build-b", created_at: "2026-10-10T08:30:00Z" })));
    await act(async () => void (await vi.advanceTimersByTimeAsync(1100)));
    const banner = await screen.findByText(/A newer build of the data was published \(build-b, 2026-10-10 08:30 UTC\)/);
    expect(screen.getByText(/build build-a/)).toBeInTheDocument(); // still showing the old one
    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(within(banner).getByRole("button", { name: "Load it" }));
    expect(screen.getByText(/build build-b/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
    expect(scene.did("destroy")).toHaveLength(1); // the old scene was torn down, a new one built
    // A failed check changes nothing: the page stays usable offline.
    fetchMock.mockImplementation(() => Promise.reject(new TypeError("offline")));
    await act(async () => void (await vi.advanceTimersByTimeAsync(1100)));
    expect(screen.getByRole("application")).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

test("keyboard: the plot pans, zooms, selects and clears without a pointer", async () => {
  const { scene } = await ready();
  const plot = screen.getByRole("application");
  plot.focus();
  await userEvent.keyboard("{ArrowLeft}{ArrowUp}+-0{Enter}{Escape}");
  expect(scene.did("pan")).toEqual([["pan", 60, 0], ["pan", 0, 60]]);
  expect(scene.did("zoom")).toEqual([["zoom", 1.5], ["zoom", 1 / 1.5]]);
  expect(scene.did("resetView")).toHaveLength(1);
  expect(scene.did("selectView")).toHaveLength(1);
  expect(scene.did("clearSelection")).toHaveLength(1);
  // Tab order reaches every control before the plot.
  await userEvent.tab({ shift: true });
  expect(screen.getByRole("button", { name: "Reset view" })).toHaveFocus();
});

test("controls drive the scene: projection, colour, tool, and the category filter", async () => {
  const { scene } = await ready();
  await userEvent.selectOptions(screen.getByLabelText("Projection"), "pca");
  expect(scene.did("setVariant")).toEqual([["setVariant", "pca"]]);
  expect(screen.getByText("This build was published without an evaluation.")).toBeInTheDocument();
  await userEvent.selectOptions(screen.getByLabelText("Projection"), "umap-n15-d0.1");
  expect(screen.getByRole("row", { name: /gain over PCA/ })).toHaveTextContent("0.2000 (0.1900 to 0.2100)");

  await userEvent.click(screen.getByRole("button", { name: "Lasso" }));
  expect(screen.getByRole("button", { name: "Lasso" })).toHaveAttribute("aria-pressed", "true");
  expect(scene.did("setTool").at(-1)).toEqual(["setTool", "lasso"]);

  const legend = screen.getByRole("group", { name: /Filter by script/ });
  expect(within(legend).getByText(/3 of 3 shown/)).toBeInTheDocument();
  await userEvent.click(within(legend).getByRole("checkbox", { name: /Arabic/ }));
  const allow = scene.did("setFilter").at(-1)![1] as Uint8Array;
  expect([allow[1], allow[2], allow[256 + 1]]).toEqual([1, 0, 1]); // Arabic (code 2) hidden, nothing else
  await userEvent.click(within(legend).getByRole("button", { name: "Show all" }));
  expect(scene.did("setFilter").at(-1)![1]).toBeNull();

  await userEvent.selectOptions(screen.getByLabelText("Colour by"), "-1");
  expect(scene.did("setColumn").at(-1)).toEqual(["setColumn", -1]);
  expect(screen.getByLabelText(/Colour scale: pale is sparse/)).toBeInTheDocument();
});

test("hover names the point in text; search finds tokens and marks them", async () => {
  const { scene } = await ready();
  await waitFor(() => expect(screen.getByRole("searchbox")).toBeEnabled());
  act(() => scene.options.onHover({ hit: { id: 7, x: 0, y: 0, cats: [2, 1, 2] }, sx: 10, sy: 10 }));
  const tip = await screen.findByRole("tooltip");
  await waitFor(() => expect(tip).toHaveTextContent("سلام"));
  expect(tip).toHaveTextContent("script: Arabic");
  expect(tip).toHaveTextContent("cluster: c02: في من");

  await userEvent.type(screen.getByRole("searchbox"), "hel{Enter}");
  expect(await screen.findByRole("button", { name: "hello" })).toBeInTheDocument();
  expect(scene.did("showMarkers").at(-1)![1]).toEqual(Uint32Array.of(5));
});

test("a WebGL failure is a readable message with a way out", async () => {
  serve(manifest);
  render(<App dataUrl="http://data.test/" createScene={() => { throw new Error("no WebGL"); }} />);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("could not start the WebGL renderer: no WebGL");
  expect(within(alert).getByRole("button", { name: "Reload" })).toHaveFocus();
});
