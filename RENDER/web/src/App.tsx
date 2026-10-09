import { useCallback, useEffect, useRef, useState } from "react";
import { Explorer } from "./Explorer";
import type { openMetaDb } from "./lib/db";
import type { SceneLike, SceneOptions } from "./lib/scene";
import { formatInt } from "./lib/stats";
import { checkManifest, type Manifest } from "./lib/types";

type State = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; manifest: Manifest };

interface Props {
  dataUrl?: string;
  /** how often to ask whether a newer build has been published */
  pollMs?: number;
  createScene?: (o: SceneOptions) => SceneLike;
  openDb?: typeof openMetaDb;
}

// Absolute, because the worker fetches tiles and would resolve a relative URL against its own script.
const DEFAULT_DATA_URL = new URL((import.meta.env.VITE_DATA_URL as string | undefined) ?? "data/", document.baseURI).href;

/** manifest.json is the one file that is never cached: it names the immutable build everything else lives under. */
async function fetchManifest(dataUrl: string): Promise<Manifest> {
  let res: Response;
  try {
    res = await fetch(`${dataUrl}manifest.json`, { cache: "no-cache" });
  } catch {
    throw new Error("The data server could not be reached. Check your connection and try again.");
  }
  if (res.status === 404) throw new Error("No dataset has been published at this address yet.");
  if (!res.ok) throw new Error(`The data server answered with an error (HTTP ${res.status}).`);
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    throw new Error("The data manifest is not valid JSON; the publish may have been interrupted.");
  }
  return checkManifest(body);
}

function defaultBudget(): number {
  const asked = Number(new URLSearchParams(location.search).get("budget"));
  if (Number.isFinite(asked) && asked >= 1000) return Math.min(asked, 20_000_000);
  // A phone gets a smaller working set; the tiles make that a sample, not a failure.
  return window.matchMedia?.("(max-width: 700px)").matches ? 750_000 : 2_000_000;
}

export function App({ dataUrl = DEFAULT_DATA_URL, pollMs = 60_000, createScene, openDb }: Props) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [newer, setNewer] = useState<Manifest | null>(null);
  const [budget] = useState(defaultBudget);
  const heading = useRef<HTMLHeadingElement>(null);

  const load = useCallback(() => {
    setState({ status: "loading" });
    fetchManifest(dataUrl).then(
      (manifest) => setState({ status: "ready", manifest }),
      (e: Error) => setState({ status: "error", message: e.message }),
    );
  }, [dataUrl]);
  useEffect(load, [load]);

  // Stale data: keep showing what is loaded, and offer the newer build rather than swapping it under the user.
  const build = state.status === "ready" ? state.manifest.build_id : null;
  useEffect(() => {
    if (!build) return;
    const check = () => {
      if (document.visibilityState === "visible") fetchManifest(dataUrl).then((m) => setNewer(m.build_id !== build ? m : null), () => undefined);
    };
    const timer = setInterval(check, pollMs);
    document.addEventListener("visibilitychange", check);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", check);
    };
  }, [build, dataUrl, pollMs]);

  const manifest = state.status === "ready" ? state.manifest : null;
  const empty = manifest && (manifest.n_points === 0 || manifest.variants.every((v) => v.n_points === 0));
  return (
    <>
      <header className="top">
        <h1 ref={heading} tabIndex={-1}>
          A Million Dots
        </h1>
        {manifest && (
          <p className="muted small">
            {formatInt(manifest.n_points)} embeddings from <strong>{manifest.source.name}</strong>
            {manifest.source.synthetic && <span className="tag warn">synthetic data</span>}
            {manifest.source.quarantined > 0 && ` · ${formatInt(manifest.source.quarantined)} rows quarantined at ingest`} · build {manifest.build_id}
          </p>
        )}
      </header>
      {newer && manifest && (
        <div className="banner" role="status">
          A newer build of the data was published ({newer.build_id}, {newer.created_at.slice(0, 16).replace("T", " ")} UTC). You are looking at {manifest.build_id}.{" "}
          <button
            onClick={() => {
              setState({ status: "ready", manifest: newer });
              setNewer(null);
              heading.current?.focus();
            }}
          >
            Load it
          </button>
        </div>
      )}
      <main>
        {state.status === "loading" && (
          <div className="state" role="status">
            Loading the dataset description…
          </div>
        )}
        {state.status === "error" && (
          <div className="state" role="alert">
            <h2>The data could not be loaded</h2>
            <p>{state.message}</p>
            <button autoFocus onClick={load}>
              Try again
            </button>
          </div>
        )}
        {manifest && empty && (
          <div className="state">
            <h2>This dataset is empty</h2>
            <p>
              The published build contains no points
              {manifest.source.quarantined > 0 ? `: all ${formatInt(manifest.source.quarantined)} input rows were quarantined at ingest.` : "."} Run the pipeline on a source with valid rows and reload.
            </p>
          </div>
        )}
        {manifest && !empty && <Explorer key={manifest.build_id} manifest={manifest} dataUrl={dataUrl} budget={budget} createScene={createScene} openDb={openDb} />}
      </main>
    </>
  );
}
