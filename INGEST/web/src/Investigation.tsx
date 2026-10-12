import type { MapViewState } from "@deck.gl/core";
import { LineLayer, PathLayer, ScatterplotLayer } from "@deck.gl/layers";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, fetchTrack, get, type DarkEvent, type Vessel } from "./api";
import { CLASS_GLYPH, CLASS_LABEL, duration, positionAt, segments, utc } from "./fleet";
import { useDeck } from "./useDeck";

const GAP_S = 1800;
const PLAY_SECONDS = 40; // a full replay takes this long, whatever the track's span

export function Investigation({ mmsi, onBack }: { mmsi: number; onBack: () => void }) {
  const [loaded, setLoaded] = useState(0);
  const vessel = useQuery({ queryKey: ["vessel", mmsi], queryFn: ({ signal }) => get<Vessel>(`/api/v1/vessels/${mmsi}`, {}, signal), retry: false });
  const track = useQuery({
    queryKey: ["track", mmsi],
    queryFn: ({ signal }) => fetchTrack(mmsi, setLoaded, signal),
    staleTime: 60_000,
    retry: false,
    enabled: vessel.isSuccess,
  });
  const events = useQuery({
    queryKey: ["events", "vessel", mmsi],
    queryFn: ({ signal }) => get<{ items: DarkEvent[] }>("/api/v1/dark-events", { mmsi, limit: 500 }, signal),
    enabled: vessel.isSuccess,
  });

  const points = track.data?.points ?? [];
  const t0 = points[0]?.[0] ?? 0;
  const t1 = points[points.length - 1]?.[0] ?? 0;
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const cursor = Math.min(t1, Math.max(t0, t || t0));
  useEffect(() => {
    if (!playing || t1 <= t0) return;
    const step = (t1 - t0) / (PLAY_SECONDS * 10);
    const id = setInterval(() => {
      setT((now) => {
        const next = Math.max(t0, now) + step;
        if (next >= t1) setPlaying(false);
        return Math.min(t1, next);
      });
    }, 100);
    return () => clearInterval(id);
  }, [playing, t0, t1]);

  const paths = useMemo(() => segments(points, GAP_S), [points]);
  const sorted = useMemo(() => [...(events.data?.items ?? [])].sort((a, b) => a.started_at.localeCompare(b.started_at)), [events.data]);
  const closed = useMemo(() => sorted.filter((e) => e.end), [sorted]);
  const here = positionAt(points, cursor);
  const hereRef = useRef(here);
  hereRef.current = here;

  const container = useRef<HTMLDivElement>(null);
  const deck = useDeck(container, {
    initial: { longitude: 10, latitude: 30, zoom: 1.5, minZoom: 0.5, maxZoom: 15 } as MapViewState,
    frame: (c) => [
      new PathLayer({ id: "track", data: paths, getPath: (d) => d, getColor: c.track, widthMinPixels: 2 }),
      // Silences are drawn as their own, thicker strokes between where the vessel vanished and where it returned.
      new LineLayer({
        id: "gaps", data: closed, getSourcePosition: (e) => e.start, getTargetPosition: (e) => e.end!, getWidth: (e) => (e.classification === "coverage_gap" ? 2 : 5),
        getColor: (e) => (e.impossible_speed ? c.jump : e.classification === "dark" ? c.dark : c.gap),
      }),
      new ScatterplotLayer({
        id: "ends", data: sorted, getPosition: (e) => e.start, stroked: true, filled: false, radiusUnits: "pixels", getRadius: 8, lineWidthMinPixels: 2,
        getLineColor: (e) => (e.classification === "dark" ? c.dark : e.classification === "position_jump" ? c.jump : c.gap),
      }),
      new ScatterplotLayer({
        id: "cursor", data: hereRef.current ? [hereRef.current] : [], getPosition: (d) => d, radiusUnits: "pixels", getRadius: 7, getFillColor: c.moving,
        stroked: true, getLineColor: [255, 255, 255], lineWidthMinPixels: 2,
      }),
    ], // prettier-ignore
  });

  // Frame the track once it arrives.
  useEffect(() => {
    if (!points.length || !deck.current) return;
    let [w, s, e, n] = [180, 90, -180, -90];
    for (const [, x, y] of points) [w, s, e, n] = [Math.min(w, x), Math.min(s, y), Math.max(e, x), Math.max(n, y)];
    const span = Math.max(e - w, (n - s) * 1.6, 0.02);
    deck.current.setProps({ initialViewState: { longitude: (w + e) / 2, latitude: (s + n) / 2, zoom: Math.min(12, Math.log2(360 / span) - 0.4), transitionDuration: 400 } as MapViewState });
  }, [points, deck]);

  const v = vessel.data;
  const notFound = vessel.error instanceof ApiError && vessel.error.status === 404;
  return (
    <div className="layout">
      <div className="map" ref={container} role="application" aria-label={`Track of vessel ${mmsi}`} />
      <aside className="panel" aria-label="Vessel investigation">
        <button className="back" onClick={onBack}>
          ← Back to the live map
        </button>
        {vessel.isPending ? (
          <p>Loading vessel {mmsi}…</p>
        ) : vessel.isError ? (
          <div role="alert" className="notice bad">
            <p>{notFound ? `No vessel with MMSI ${mmsi} has been seen.` : `The vessel could not be loaded. ${vessel.error.message}`}</p>
            {!notFound && <button onClick={() => vessel.refetch()}>Try again</button>}
          </div>
        ) : (
          v && (
            <>
              <h2>{v.name ?? "Unnamed vessel"}</h2>
              <dl>
                <dt>MMSI</dt>
                <dd>{v.mmsi}</dd>
                <dt>Flag</dt>
                <dd>{v.country ? `${v.country} (${v.flag})` : "Unallocated MID"}</dd>
                <dt>IMO</dt>
                <dd>{v.imo ?? "not reported or invalid"}</dd>
                <dt>Call sign</dt>
                <dd>{v.callsign ?? "not reported"}</dd>
                <dt>Size</dt>
                <dd>{v.length_m ? `${v.length_m} × ${v.beam_m ?? "?"} m` : "not reported"}</dd>
                <dt>Last heard</dt>
                <dd>{v.last_seen ? utc(v.last_seen) : "never"}</dd>
              </dl>

              <section aria-live="polite">
                {track.isPending ? (
                  <p>
                    Loading track… <strong>{loaded.toLocaleString()}</strong> positions so far
                  </p>
                ) : track.isError ? (
                  <div role="alert" className="notice bad">
                    <p>The track could not be loaded. {track.error.message}</p>
                    <button onClick={() => track.refetch()}>Try again</button>
                  </div>
                ) : points.length === 0 ? (
                  <p className="notice">This vessel has identity data but no stored positions.</p>
                ) : (
                  <>
                    {track.data.error && (
                      <div role="alert" className="notice bad">
                        <p>Only the first {points.length.toLocaleString()} positions loaded; the rest failed. {track.data.error.message}</p>
                        <button onClick={() => track.refetch()}>Load the rest</button>
                      </div>
                    )}
                    <div className="replay">
                      <button onClick={() => (cursor >= t1 ? (setT(t0), setPlaying(true)) : setPlaying((p) => !p))} aria-pressed={playing}>
                        {playing ? "❚❚ Pause" : cursor >= t1 ? "↺ Replay" : "▶ Play"}
                      </button>
                      <label>
                        Replay position: <output>{utc(cursor)}</output>
                        <input
                          // Thousandths of the track, not seconds: every track gets the same keyboard granularity,
                          // and Home/End land exactly on its first and last fix.
                          type="range" min={0} max={1000} step={1} value={t1 > t0 ? Math.round(((cursor - t0) / (t1 - t0)) * 1000) : 0}
                          aria-valuetext={utc(cursor)}
                          onChange={(e) => (setPlaying(false), setT(t0 + (Number(e.target.value) / 1000) * (t1 - t0)))}
                        />
                      </label>
                      <p>
                        {points.length.toLocaleString()} positions over {duration(t1 - t0)}, in {paths.length} unbroken stretch{paths.length === 1 ? "" : "es"}.
                      </p>
                    </div>
                  </>
                )}
              </section>

              <section className="events">
                <h3>Silences on record</h3>
                {events.isPending ? (
                  <p>Loading events…</p>
                ) : events.isError ? (
                  <div role="alert" className="notice bad">
                    <p>Events could not be loaded. {events.error.message}</p>
                    <button onClick={() => events.refetch()}>Try again</button>
                  </div>
                ) : sorted.length === 0 ? (
                  <p className="notice">None. This vessel has reported without unexplained gaps.</p>
                ) : (
                  <ol>
                    {sorted.map((e) => (
                      <li key={e.id}>
                        <button className="event" onClick={() => (setPlaying(false), setT(Date.parse(e.started_at) / 1000))}>
                          <span className={`glyph ${e.classification}`} aria-hidden="true">
                            {CLASS_GLYPH[e.classification]}
                          </span>
                          <span className="who">
                            {CLASS_LABEL[e.classification]}
                            {e.impossible_speed && ", impossible speed"}
                            <small>
                              {utc(e.started_at)} · {duration(e.duration_s)}
                              {!e.ended_at && ", ongoing"}
                            </small>
                          </span>
                          <span className="score" aria-label={`score ${e.score}`}>
                            {e.score.toFixed(0)}
                          </span>
                        </button>
                        <ul className="detail">
                          {e.reasons.map((r) => (
                            <li key={r}>{r}</li>
                          ))}
                        </ul>
                      </li>
                    ))}
                  </ol>
                )}
              </section>
            </>
          )
        )}
      </aside>
    </div>
  );
}
