import type { MapViewState, PickingInfo } from "@deck.gl/core";
import { LineLayer, PathLayer, ScatterplotLayer, SolidPolygonLayer } from "@deck.gl/layers";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, get, type Classification, type Coverage, type DarkEvent, type Live, type VesselHit } from "./api";
import { ago, CLASS_GLYPH, CLASS_LABEL, duration, Fleet, utc } from "./fleet";
import { bboxOf, useDeck } from "./useDeck";

const POLL_MS = 5_000;
const STALE_AFTER_S = 120;
const TRAILS_FROM_ZOOM = 5.5;

interface LiveState {
  phase: "loading" | "ready" | "error";
  count: number;
  dataTime: number | null; // unix seconds of the newest fix in the system
  fetchedAt: number; // Date.now() of the last successful poll
  truncated: boolean;
  error: ApiError | null;
  version: number;
}

/** Poll the live endpoint, asking only for what changed, and fold it into the fleet buffers. */
function useLiveFleet(fleet: Fleet, enabled: boolean): LiveState & { retry: () => void } {
  const [state, setState] = useState<LiveState>({ phase: "loading", count: 0, dataTime: null, fetchedAt: 0, truncated: false, error: null, version: 0 });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    const abort = new AbortController();
    let timer = 0;
    let since: string | undefined;
    const poll = async () => {
      try {
        const live = await get<Live>("/api/v1/vessels/live", { since }, abort.signal);
        fleet.upsert(live.vessels);
        // Overlap by a few seconds: a row committed just after the server read its clock must not be missed.
        since = new Date(Date.parse(live.server_time) - 5000).toISOString();
        setState((s) => ({
          phase: "ready", count: fleet.size, dataTime: live.data_time ? Date.parse(live.data_time) / 1000 : null, fetchedAt: Date.now(),
          truncated: s.truncated || live.truncated, error: null, version: s.version + 1,
        })); // prettier-ignore
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        // Keep showing what we have; say that it is no longer fresh.
        setState((s) => ({ ...s, phase: s.count ? "ready" : "error", error: e as ApiError }));
      }
      timer = window.setTimeout(poll, document.hidden ? POLL_MS * 6 : POLL_MS);
    };
    void poll();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [fleet, enabled, attempt]);
  return { ...state, retry: () => setAttempt((n) => n + 1) };
}

function benchFleet(n: number): Fleet {
  const fleet = new Fleet();
  const now = Date.now() / 1000;
  let seed = 42;
  const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
  fleet.upsert(Array.from({ length: n }, (_, i) => [200_000_000 + i, rnd() * 360 - 180, rnd() * 140 - 70, 5 + rnd() * 20, rnd() * 360, null, now]));
  return fleet;
}

export function LiveMap({ onOpenVessel }: { onOpenVessel: (mmsi: number) => void }) {
  const bench = useMemo(() => Number(new URLSearchParams(location.search).get("bench")) || 0, []);
  const fleet = useMemo(() => (bench ? benchFleet(bench) : new Fleet()), [bench]);
  const live = useLiveFleet(fleet, !bench);
  const [classification, setClassification] = useState<Classification | "">("dark");
  const [minScore, setMinScore] = useState(0);
  const [showCoverage, setShowCoverage] = useState(false);
  const [selected, setSelected] = useState<DarkEvent | null>(null);
  const [view, setView] = useState<{ bbox: string; zoom: number } | null>(null);
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setClock(Date.now()), 5000);
    return () => clearInterval(id);
  }, []);

  const events = useQuery({
    queryKey: ["events", classification, minScore],
    queryFn: ({ signal }) => get<{ items: DarkEvent[] }>("/api/v1/dark-events", { classification, min_score: minScore, limit: 200 }, signal),
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });
  const coverage = useQuery({
    queryKey: ["coverage"],
    queryFn: ({ signal }) => get<Coverage>("/api/v1/coverage", {}, signal),
    enabled: showCoverage,
    staleTime: 300_000,
  });
  const trailsOn = !!view && view.zoom >= TRAILS_FROM_ZOOM && !bench;
  const trails = useQuery({
    queryKey: ["trails", view?.bbox],
    queryFn: ({ signal }) => get<{ trails: Record<string, [number, number, number][]> }>("/api/v1/vessels/trails", { bbox: view!.bbox, minutes: 90 }, signal),
    enabled: trailsOn,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });

  // Everything the frame callback reads lives in refs or memos: it runs 60 times a second.
  const items = events.data?.items ?? [];
  const jumps = useMemo(() => items.filter((e) => e.end && e.impossible_speed), [items]);
  const paths = useMemo(() => (trailsOn && trails.data ? Object.values(trails.data.trails).map((t) => t.map(([x, y]) => [x, y])) : []), [trails.data, trailsOn]);
  const cells = useMemo(() => {
    const size = coverage.data?.cell_deg ?? 0;
    return showCoverage && size ? coverage.data!.cells.map(([cx, cy, r]) => ({ r, ring: [[cx * size, cy * size], [(cx + 1) * size, cy * size], [(cx + 1) * size, (cy + 1) * size], [cx * size, (cy + 1) * size]] })) : [];
  }, [coverage.data, showCoverage]); // prettier-ignore
  const style = useMemo(() => {
    // Moving vessels are larger as well as differently coloured; rebuilt only when the fleet changes.
    const radius = new Float32Array(fleet.size);
    for (let i = 0; i < fleet.size; i++) radius[i] = fleet.sog[i] >= 0.5 ? 3.5 : 2;
    return radius;
  }, [fleet, live.version]);
  const buffer = useRef(new Float32Array(0));
  const dataNow = () => (bench || live.dataTime === null ? Date.now() / 1000 : live.dataTime + (Date.now() - live.fetchedAt) / 1000);

  const container = useRef<HTMLDivElement>(null);
  const viewTimer = useRef(0);
  const frames = useMemo(() => (bench ? ((window as unknown as { __frames: number[] }).__frames = []) : undefined), [bench]);
  const deck = useDeck(container, {
    initial: { longitude: 20, latitude: 25, zoom: 1.4, minZoom: 0.5, maxZoom: 14 } as MapViewState,
    frameLog: frames,
    frame: (c) => {
      const n = Math.min(fleet.size, style.length);
      if (buffer.current.length < 2 * fleet.size) buffer.current = new Float32Array(2 * fleet.mmsi.length);
      fleet.positionsAt(dataNow(), buffer.current);
      return [
        new SolidPolygonLayer({ id: "coverage", data: cells, getPolygon: (d) => d.ring, getFillColor: (d) => [...c.coverage, 25 + d.r * 80] as [number, number, number, number] }),
        new PathLayer({ id: "trails", data: paths, getPath: (d) => d as [number, number][], getColor: [...c.track, 110], widthMinPixels: 1 }),
        new ScatterplotLayer({
          id: "vessels", pickable: true, radiusUnits: "pixels", getFillColor: c.moving,
          data: { length: n, attributes: { getPosition: { value: buffer.current.subarray(0, 2 * n), size: 2 }, getRadius: { value: style, size: 1 } } },
        }),
        new LineLayer({ id: "jumps", data: jumps, getSourcePosition: (e) => e.start, getTargetPosition: (e) => e.end!, getColor: c.jump, getWidth: 2 }),
        // Events are hollow rings, vessels are solid dots: told apart by shape, not only by colour.
        new ScatterplotLayer({
          id: "events", data: items, pickable: true, stroked: true, filled: false, radiusUnits: "pixels", lineWidthMinPixels: 2,
          getPosition: (e) => e.start, getRadius: (e) => 6 + e.score / 10 + (e.id === selected?.id ? 5 : 0),
          getLineColor: (e) => (e.classification === "dark" ? c.dark : e.classification === "position_jump" ? c.jump : c.gap),
          updateTriggers: { getRadius: selected?.id },
        }),
      ]; // prettier-ignore
    },
    onClick: (info: PickingInfo) => {
      if (info.layer?.id === "events" && info.object) setSelected(info.object as DarkEvent);
      else if (info.layer?.id === "vessels" && info.index >= 0) onOpenVessel(fleet.mmsi[info.index]);
    },
    tooltip: (info) => {
      if (info.layer?.id === "events" && info.object) {
        const e = info.object as DarkEvent;
        return `${e.name ?? e.mmsi}: ${CLASS_LABEL[e.classification]}, score ${e.score}`;
      }
      if (info.layer?.id === "vessels" && info.index >= 0) return `MMSI ${fleet.mmsi[info.index]}, ${fleet.sog[info.index].toFixed(1)} kn. Click to investigate.`;
      return null;
    },
    onView: (v) => {
      clearTimeout(viewTimer.current); // settle before fetching: one request per pan, not one per frame
      viewTimer.current = window.setTimeout(() => setView({ bbox: bboxOf(v), zoom: v.zoom }), 400);
    },
  });

  const select = (e: DarkEvent) => {
    setSelected(e);
    deck.current?.setProps({ initialViewState: { longitude: e.start[0], latitude: e.start[1], zoom: 6, transitionDuration: 500 } as MapViewState });
  };

  const staleFor = live.fetchedAt ? (clock - live.fetchedAt) / 1000 : 0;
  const feedAge = live.dataTime !== null ? clock / 1000 - live.dataTime : 0;

  return (
    <div className="layout">
      <div className="map" ref={container} role="application" aria-label="World map of vessels. Use the ranked list beside it for keyboard access to events." />
      <aside className="panel" aria-label="Fleet status and ranked dark events">
        <section aria-live="polite" className="status">
          {bench ? (
            <p>Benchmark mode: {bench.toLocaleString()} synthetic vessels, no server data.</p>
          ) : live.phase === "loading" ? (
            <p>Loading the fleet…</p>
          ) : live.phase === "error" ? (
            <div role="alert" className="notice bad">
              <p>The fleet could not be loaded. {live.error?.message}</p>
              <button onClick={live.retry}>Try again</button>
            </div>
          ) : (
            <>
              <p>
                <strong>{live.count.toLocaleString()}</strong> vessels tracked
                {live.dataTime !== null && <>, newest position {utc(live.dataTime)}</>}
              </p>
              {live.count === 0 && <p className="notice">No positions have been ingested yet. Check that a collector is running.</p>}
              {live.error && staleFor > POLL_MS / 500 && (
                <p role="alert" className="notice bad">
                  Showing data from {ago(staleFor)}: updates are failing. {live.error.message}
                </p>
              )}
              {!live.error && feedAge > STALE_AFTER_S && live.count > 0 && (
                <p className="notice">The feed itself is stale: nothing new for {duration(feedAge)}. Positions shown are last known.</p>
              )}
              {live.truncated && <p className="notice">More vessels exist than one request returns; the map shows a partial fleet.</p>}
            </>
          )}
        </section>

        <VesselSearch onOpen={onOpenVessel} />

        <fieldset className="filters">
          <legend>Ranked events</legend>
          <label>
            Kind
            <select value={classification} onChange={(e) => setClassification(e.target.value as Classification | "")}>
              <option value="">All</option>
              <option value="dark">Went dark</option>
              <option value="coverage_gap">Coverage gap</option>
              <option value="position_jump">Position jump</option>
            </select>
          </label>
          <label>
            Minimum score: {minScore}
            <input type="range" min={0} max={100} step={5} value={minScore} onChange={(e) => setMinScore(Number(e.target.value))} />
          </label>
          <label className="check">
            <input type="checkbox" checked={showCoverage} onChange={(e) => setShowCoverage(e.target.checked)} />
            Show receiver coverage
          </label>
          {showCoverage && coverage.isError && <p className="notice bad">Coverage could not be loaded.</p>}
          {showCoverage && coverage.data?.cells.length === 0 && <p className="notice">No coverage has been computed yet.</p>}
        </fieldset>

        <section aria-busy={events.isFetching} className="events">
          {events.isPending ? (
            <p>Loading ranked events…</p>
          ) : events.isError && !events.data ? (
            <div role="alert" className="notice bad">
              <p>Events could not be loaded. {events.error.message}</p>
              <button onClick={() => events.refetch()}>Try again</button>
            </div>
          ) : items.length === 0 ? (
            <p className="notice">No events match these filters.</p>
          ) : (
            <>
              {events.isError && <p className="notice bad">Could not refresh; this list may be out of date.</p>}
              <ol>
                {items.map((e) => (
                  <li key={e.id}>
                    <button className="event" aria-pressed={selected?.id === e.id} onClick={() => select(e)}>
                      <span className={`glyph ${e.classification}`} aria-hidden="true">
                        {CLASS_GLYPH[e.classification]}
                      </span>
                      <span className="who">
                        {e.name ?? `MMSI ${e.mmsi}`} {e.flag && <small>{e.flag}</small>}
                        <small>
                          {CLASS_LABEL[e.classification]}
                          {e.impossible_speed && ", impossible speed"} · {duration(e.duration_s)}
                          {!e.ended_at && ", ongoing"}
                        </small>
                      </span>
                      <span className="score" aria-label={`score ${e.score}`}>
                        {e.score.toFixed(0)}
                      </span>
                    </button>
                    {selected?.id === e.id && (
                      <div className="detail">
                        <p>
                          Vanished {utc(e.started_at)}
                          {e.ended_at ? `, returned ${utc(e.ended_at)}` : ", not yet heard again"}.
                        </p>
                        <ul>
                          {e.reasons.map((r) => (
                            <li key={r}>{r}</li>
                          ))}
                        </ul>
                        <button onClick={() => onOpenVessel(e.mmsi)}>Investigate this vessel's track</button>
                      </div>
                    )}
                  </li>
                ))}
              </ol>
            </>
          )}
        </section>

        <section className="legend" aria-label="Map legend">
          <p>
            <span className="glyph moving">●</span> solid dot: vessel (larger when under way)
          </p>
          <p>
            <span className="glyph dark">◯</span> ring: went dark · <span className="glyph coverage_gap">◯</span> grey ring: coverage gap
          </p>
          <p>
            <span className="glyph position_jump">╱</span> line: reappearance that needed an impossible speed
          </p>
          <p>Zoom in to see trails. Arrow keys pan the map, + and − zoom.</p>
        </section>
      </aside>
    </div>
  );
}

function VesselSearch({ onOpen }: { onOpen: (mmsi: number) => void }) {
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setQuery(text.trim()), 250); // not on every keystroke
    return () => clearTimeout(id);
  }, [text]);
  const hits = useQuery({
    queryKey: ["search", query],
    queryFn: ({ signal }) => get<VesselHit[]>("/api/v1/vessels/search", { q: query }, signal),
    enabled: query.length >= 2,
    staleTime: 60_000,
  });
  return (
    <form
      className="search"
      role="search"
      onSubmit={(e) => {
        e.preventDefault();
        if (/^\d{9}$/.test(text.trim())) onOpen(Number(text.trim()));
        else if (hits.data?.[0]) onOpen(hits.data[0].mmsi);
      }}
    >
      <label>
        Find a vessel
        <input type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder="Name or MMSI" autoComplete="off" />
      </label>
      {query.length >= 2 &&
        (hits.isPending ? (
          <p>Searching…</p>
        ) : hits.isError ? (
          <p className="notice bad">Search failed. {hits.error.message}</p>
        ) : hits.data.length === 0 ? (
          <p className="notice">No vessel matches “{query}”.</p>
        ) : (
          <ul>
            {hits.data.map((h) => (
              <li key={h.mmsi}>
                <button type="button" onClick={() => onOpen(h.mmsi)}>
                  {h.name ?? "Unnamed"} <small>{h.mmsi}{h.flag ? ` · ${h.flag}` : ""}</small>
                </button>
              </li>
            ))}
          </ul>
        ))}
    </form>
  );
}
