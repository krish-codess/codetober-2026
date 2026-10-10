import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Legend } from "./components/Legend";
import { StatsPanel, type MetaState } from "./components/StatsPanel";
import type { MetaDb, MetaSummary, TokenRow, openMetaDb } from "./lib/db";
import { Scene, type HoverInfo, type SceneLike, type SceneOptions, type SceneStatus, type SelectionInfo, type Tool } from "./lib/scene";
import { formatInt } from "./lib/stats";
import type { ThemeName } from "./lib/theme";
import type { Interval, Manifest } from "./lib/types";

interface Props {
  manifest: Manifest;
  dataUrl: string;
  budget: number;
  createScene?: (o: SceneOptions) => SceneLike;
  openDb?: typeof openMetaDb;
}

function useTheme(): ThemeName {
  const query = useMemo(() => window.matchMedia?.("(prefers-color-scheme: dark)"), []);
  const [dark, setDark] = useState(!!query?.matches);
  useEffect(() => {
    const on = (e: MediaQueryListEvent) => setDark(e.matches);
    query?.addEventListener("change", on);
    return () => query?.removeEventListener("change", on);
  }, [query]);
  return dark ? "dark" : "light";
}

const TOOLS: { tool: Tool; label: string; hint: string }[] = [
  { tool: "pan", label: "Pan", hint: "Drag to move, scroll or pinch to zoom" },
  { tool: "lasso", label: "Lasso", hint: "Drag a freehand outline around points" },
  { tool: "box", label: "Box", hint: "Drag a rectangle around points" },
];

// DuckDB-WASM is a separate chunk, fetched only after the first frame is on screen.
const lazyOpenDb: typeof openMetaDb = async (...args) => (await import("./lib/db")).openMetaDb(...args);

const ci = (v: Interval | null | undefined): string => (v ? `${v.mean.toFixed(4)} (${v.lo.toFixed(4)} to ${v.hi.toFixed(4)})` : "n/a");

export function Explorer({ manifest, dataUrl, budget, createScene, openDb = lazyOpenDb }: Props) {
  const theme = useTheme();
  const canvas = useRef<HTMLCanvasElement>(null);
  const overlay = useRef<HTMLCanvasElement>(null);
  const scene = useRef<SceneLike | null>(null);
  const db = useRef<MetaDb | null>(null);

  const [variant, setVariant] = useState(manifest.variants[manifest.variants.length > 1 ? 1 : 0].name);
  const [column, setColumn] = useState(0);
  const [tool, setTool] = useState<Tool>("pan");
  const [hidden, setHidden] = useState<Record<number, ReadonlySet<number>>>({});
  const [status, setStatus] = useState<SceneStatus | null>(null);
  const [selection, setSelection] = useState<SelectionInfo | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const [hoverRow, setHoverRow] = useState<TokenRow | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [meta, setMeta] = useState<MetaState>({ status: "loading", progress: 0 });
  const [metaTry, setMetaTry] = useState(0);
  const [tokens, setTokens] = useState<MetaSummary | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<TokenRow[] | null>(null);

  const current = manifest.variants.find((v) => v.name === variant)!;
  const filterColumn = column >= 0 ? column : 0;

  // The scene lives as long as this build of the data does.
  useEffect(() => {
    if (!canvas.current || !overlay.current) return;
    const options: SceneOptions = { canvas: canvas.current, overlay: overlay.current, manifest, dataUrl, variant, budget, theme, onStatus: setStatus, onSelection: setSelection, onHover: setHover, onError: setFatal };
    try {
      scene.current = createScene ? createScene(options) : new Scene(options);
    } catch (e) {
      setFatal(`This browser could not start the WebGL renderer: ${(e as Error).message}`);
      return;
    }
    (window as unknown as { __dots?: unknown }).__dots = scene.current; // the benchmark and end-to-end test drive the page through this
    return () => {
      scene.current?.destroy();
      scene.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- later changes go through the setters below
  }, [manifest, dataUrl, budget, createScene]);

  useEffect(() => scene.current?.setTheme(theme), [theme]);
  useEffect(() => scene.current?.setColumn(column), [column]);
  useEffect(() => scene.current?.setTool(tool), [tool]);
  useEffect(() => {
    const active = manifest.columns.some((_, ci) => hidden[ci]?.size);
    let allow: Uint8Array | null = null;
    if (active) {
      allow = new Uint8Array(3 * 256).fill(1);
      manifest.columns.forEach((_, ci) => hidden[ci]?.forEach((code) => (allow![ci * 256 + code] = 0)));
    }
    scene.current?.setFilter(allow);
  }, [hidden, manifest]);

  const changeVariant = (name: string) => {
    setVariant(name);
    setResults(null);
    scene.current?.setVariant(name);
  };

  // The metadata table starts downloading once there is something on screen, never before.
  const painted = status?.firstPaintMs != null;
  useEffect(() => {
    if (!painted) return;
    let cancelled = false;
    setMeta({ status: "loading", progress: 0 });
    openDb(dataUrl + manifest.meta.path, manifest.meta.bytes, (p) => !cancelled && setMeta({ status: "loading", progress: p }))
      .then((opened) => {
        if (cancelled) return void opened.close();
        db.current = opened;
        setMeta({ status: "ready" });
      })
      .catch((e: Error) => !cancelled && setMeta({ status: "error", message: e.message }));
    return () => {
      cancelled = true;
      void db.current?.close();
      db.current = null;
    };
  }, [painted, dataUrl, manifest, openDb, metaTry]);

  // Token statistics for a finished selection, from DuckDB-WASM.
  const ids = selection && !selection.live ? selection.ids : null;
  useEffect(() => {
    setTokens(null);
    if (!ids || meta.status !== "ready" || !db.current) return;
    let cancelled = false;
    db.current
      .summary(ids)
      .then((s) => !cancelled && setTokens(s))
      .catch((e: Error) => !cancelled && setMeta({ status: "error", message: e.message }));
    return () => {
      cancelled = true;
    };
  }, [ids, meta.status]);

  const hoverId = hover?.hit.id;
  useEffect(() => {
    setHoverRow(null);
    if (hoverId == null || !db.current) return;
    let cancelled = false;
    db.current.lookup(hoverId).then((r) => !cancelled && setHoverRow(r), () => undefined);
    return () => {
      cancelled = true;
    };
  }, [hoverId]);

  const search = useCallback(async () => {
    if (!db.current || !query.trim()) return setResults(null);
    try {
      const rows = await db.current.search(query.trim());
      setResults(rows);
      scene.current?.showMarkers(Uint32Array.from(rows, (r) => r.id));
    } catch (e) {
      setMeta({ status: "error", message: (e as Error).message });
    }
  }, [query]);

  const onKey = (e: KeyboardEvent) => {
    const s = scene.current;
    if (!s) return;
    const step = e.shiftKey ? 200 : 60;
    const actions: Record<string, () => void> = {
      ArrowLeft: () => s.pan(step, 0),
      ArrowRight: () => s.pan(-step, 0),
      ArrowUp: () => s.pan(0, step),
      ArrowDown: () => s.pan(0, -step),
      "+": () => s.zoom(1.5),
      "=": () => s.zoom(1.5),
      "-": () => s.zoom(1 / 1.5),
      "0": () => s.resetView(),
      Enter: () => s.selectView(),
      Escape: () => s.clearSelection(),
    };
    const act = actions[e.key];
    if (act) {
      e.preventDefault();
      act();
    }
  };

  const toggle = (code: number) =>
    setHidden((h) => {
      const next = new Set(h[filterColumn]);
      if (!next.delete(code)) next.add(code);
      return { ...h, [filterColumn]: next };
    });
  const all = (show: boolean) => setHidden((h) => ({ ...h, [filterColumn]: new Set(show ? [] : manifest.columns[filterColumn].labels.map((_, c) => c).filter((c) => manifest.columns[filterColumn].counts[c] > 0)) }));

  if (fatal)
    return (
      <div className="state" role="alert">
        <h2>The plot stopped</h2>
        <p>{fatal}</p>
        <button autoFocus onClick={() => location.reload()}>
          Reload
        </button>
      </div>
    );

  const loadingTiles = status ? status.tilesWanted - status.tilesLoaded - status.failed.length : 0;
  const m = current.metrics;
  return (
    <div className="explorer">
      <div className="toolbar" role="toolbar" aria-label="Plot controls">
        <label>
          Projection
          <select value={variant} onChange={(e) => changeVariant(e.target.value)}>
            {manifest.variants.map((v) => (
              <option key={v.name} value={v.name}>
                {v.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Colour by
          <select value={column} onChange={(e) => setColumn(Number(e.target.value))}>
            <option value={-1}>density only</option>
            {manifest.columns.map((c, i) => (
              <option key={c.name} value={i}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <div className="group" role="group" aria-label="Pointer tool">
          {TOOLS.map((t) => (
            <button key={t.tool} aria-pressed={tool === t.tool} title={t.hint} onClick={() => setTool(t.tool)}>
              {t.label}
            </button>
          ))}
        </div>
        <button onClick={() => scene.current?.selectView()}>Select view</button>
        <button onClick={() => scene.current?.clearSelection()} disabled={!selection}>
          Clear
        </button>
        <button onClick={() => scene.current?.resetView()}>Reset view</button>
      </div>

      <div className="stage">
        <div
          className={`plot tool-${tool}`}
          tabIndex={0}
          role="application"
          aria-label={`Scatter plot of ${formatInt(current.n_points)} points. Arrow keys pan, plus and minus zoom, 0 resets the view, Enter selects everything in view, Escape clears the selection.`}
          onKeyDown={onKey}
        >
          <canvas ref={canvas} data-testid="plot" />
          <canvas ref={overlay} className="overlay" aria-hidden="true" />
          {!painted && (
            <div className="veil" role="status">
              Loading the first {formatInt(Math.min(manifest.tile_capacity, current.n_points))} points…
            </div>
          )}
          {hover && (
            <div className="tip" style={{ left: hover.sx, top: hover.sy }} role="tooltip">
              <strong>
                <bdi>{hoverRow?.token ?? `row ${hover.hit.id}`}</bdi>
              </strong>
              {hoverRow && <span className="muted"> #{formatInt(hoverRow.rank)}</span>}
              {manifest.columns.map((c, i) => (
                <div key={c.name} className="small">
                  {c.name}: {c.labels[hover.hit.cats[i]] ?? "?"}
                </div>
              ))}
            </div>
          )}
          {column < 0 && (
            <div className="ramp" aria-label="Colour scale: pale is sparse, saturated is dense, on a logarithmic scale">
              <span>sparse</span>
              <i />
              <span>dense (log)</span>
            </div>
          )}
        </div>

        <aside className="side">
          <StatsPanel columns={manifest.columns} total={current.n_points} selection={selection} meta={meta} tokens={tokens} onRetryMeta={() => setMetaTry((n) => n + 1)} />

          <Legend column={manifest.columns[filterColumn]} theme={theme} coloured={column >= 0} hidden={hidden[filterColumn] ?? new Set()} onToggle={toggle} onAll={all} />

          <section className="panel" aria-labelledby="find-h">
            <h2 id="find-h">Find a token</h2>
            <form
              className="row"
              onSubmit={(e) => {
                e.preventDefault();
                void search();
              }}
            >
              <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} maxLength={80} aria-label="Token contains" placeholder="contains…" disabled={meta.status !== "ready"} />
              <button disabled={meta.status !== "ready"}>Find</button>
            </form>
            {meta.status === "loading" && <p className="muted small">Available once the token table has loaded ({Math.round(meta.progress * 100)}%).</p>}
            {meta.status === "error" && !selection && (
              <p className="note" role="alert">
                The token table could not be loaded ({meta.message}), so search and token statistics are off. The plot and selection counts still work. <button onClick={() => setMetaTry((n) => n + 1)}>Retry</button>
              </p>
            )}
            {results && results.length === 0 && <p className="muted small">No token contains “{query}”.</p>}
            {results && results.length > 0 && (
              <ul className="results">
                {results.slice(0, 12).map((r) => (
                  <li key={r.id}>
                    <button
                      className="link"
                      onClick={() => scene.current?.showMarkers(Uint32Array.of(r.id))}
                    >
                      <bdi>{r.token}</bdi>
                    </button>{" "}
                    <span className="muted small">
                      #{formatInt(r.rank)} · {r.script ?? "no letters"} · {r.kind}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {results && results.length > 0 && <p className="muted small">Matches in the loaded tiles are circled on the plot.</p>}
          </section>

          <section className="panel" aria-labelledby="q-h">
            <h2 id="q-h">How faithful is this projection?</h2>
            {m ? (
              <table className="metrics">
                <tbody>
                  <tr>
                    <th scope="row">Neighbour recall@{manifest.eval.k}</th>
                    <td>{ci(m.recall.all)}</td>
                  </tr>
                  {m.recall.placed && (
                    <tr>
                      <th scope="row">rows placed after fitting</th>
                      <td>{ci(m.recall.placed)}</td>
                    </tr>
                  )}
                  {m.recall_vs_pca && (
                    <tr>
                      <th scope="row">gain over PCA</th>
                      <td>{ci(m.recall_vs_pca)}</td>
                    </tr>
                  )}
                  {manifest.columns.map((c) => (
                    <tr key={c.name}>
                      <th scope="row">{c.name} purity</th>
                      <td>
                        {ci(m.purity[c.name])}
                        <span className="muted small"> chance {manifest.eval.chance?.purity[c.name]?.toFixed(4)}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted small">This build was published without an evaluation.</p>
            )}
            <p className="muted small">Mean with 95% bootstrap interval over sampled rows. Recall: share of a row's nearest neighbours in {manifest.source.name.includes("synth") ? "the original space" : "50-d"} that are still its nearest in 2-D.</p>
          </section>
        </aside>
      </div>

      <footer className="status">
        {status && status.failed.length > 0 && (
          <p className="note" role="alert">
            <strong>
              {status.failed.length} tile{status.failed.length > 1 ? "s" : ""} could not be loaded
            </strong>{" "}
            after several attempts ({status.failed[0].message}). The plot is missing those points. <button onClick={() => scene.current?.retryFailed()}>Retry</button>
          </p>
        )}
        <p role="status" aria-live="polite" className="small muted">
          {status ? (
            <>
              Drawing {formatInt(status.pointsDrawn)}
              {status.pointsInView != null && ` of about ${formatInt(Math.max(status.pointsInView, status.pointsDrawn))}`} points in view
              {loadingTiles > 0 && (
                <>
                  {" · "}loading tiles <progress max={status.tilesWanted} value={status.tilesLoaded} aria-label="tiles loaded" /> {status.tilesLoaded}/{status.tilesWanted}
                </>
              )}
              {" · "}
              {formatInt(status.pointsHeld)} held in memory (cap {formatInt(budget * 1.25)})
              {status.fps != null && ` · ${status.fps.toFixed(0)} fps`}
              {status.shading === "8-bit (degraded)" && " · this GPU has no float render target, so dense areas saturate early"}
            </>
          ) : (
            "Starting…"
          )}
        </p>
      </footer>
    </div>
  );
}
