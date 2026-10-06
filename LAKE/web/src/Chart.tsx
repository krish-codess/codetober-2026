// The tradeoff chart: file size vs write time vs query time, one mark per variant.
// Two of the three sit on log axes; the third is mark area. Format is carried by shape AND
// colour, so the chart reads in greyscale and under colour-vision deficiency.
import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { fmtBytes, fmtSeconds, type QueryDef, type Variant } from "./api";

type Format = Variant["format"];
export const FORMATS: { id: Format; label: string }[] = [
  { id: "parquet", label: "Parquet" }, { id: "orc", label: "ORC" }, { id: "avro", label: "Avro" }, { id: "csv", label: "CSV" },
];
type XKey = "size" | "write";
type YKey = "warm" | "cold" | "read";

const W = 720, H = 430, M = { l: 64, r: 20, t: 16, b: 46 };

export function Shape({ format, r }: { format: Format; r: number }) {
  if (format === "orc") return <rect x={-r * 0.89} y={-r * 0.89} width={r * 1.78} height={r * 1.78} rx={1.5} />;
  if (format === "avro") return <path d={`M0,${-r * 1.2} L${r * 1.1},${r * 0.8} L${-r * 1.1},${r * 0.8} Z`} />;
  if (format === "csv") return <path d={`M0,${-r * 1.25} L${r * 1.25},0 L0,${r * 1.25} L${-r * 1.25},0 Z`} />;
  return <circle r={r} />;
}

function logTicks(lo: number, hi: number): number[] {
  const out: number[] = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++)
    for (const m of [1, 2, 5]) { const v = m * 10 ** e; if (v >= lo && v <= hi) out.push(v); }
  return out.length > 7 ? out.filter((v) => /^1/.test(v.toExponential())) : out;
}

function scale(values: number[], a: number, b: number) {
  const lo = Math.min(...values) * 0.75, hi = Math.max(...values) * 1.35;
  const span = Math.log(hi / lo) || 1;
  return { lo, hi, at: (v: number) => a + (Math.log(v / lo) / span) * (b - a) };
}

interface Props { variants: Variant[]; queries: QueryDef[]; selected: string | null; onSelect: (id: string) => void }

export function TradeoffChart({ variants, queries, selected, onSelect }: Props) {
  const [xKey, setXKey] = useState<XKey>("size");
  const [yKey, setYKey] = useState<YKey>("warm");
  const [queryId, setQueryId] = useState(queries.find((q) => q.id === "projection")?.id ?? queries[0]?.id ?? "");
  const [hidden, setHidden] = useState<Set<Format>>(new Set());
  const [active, setActive] = useState<string | null>(null);
  const [focusIx, setFocusIx] = useState(0);
  const marks = useRef<(SVGGElement | null)[]>([]);

  const points = useMemo(() => {
    const out = [];
    for (const v of variants) {
      const cell = v.queries[queryId];
      const y = yKey === "read" ? cell?.bytes_read : cell?.[yKey]?.median_s;
      if (hidden.has(v.format) || y == null || !(y > 0)) continue;
      out.push({ v, x: xKey === "size" ? v.bytes : v.write_s, y, area: xKey === "size" ? v.write_s : v.bytes });
    }
    return out.sort((a, b) => a.x - b.x || a.y - b.y);
  }, [variants, queryId, xKey, yKey, hidden]);
  const unplotted = variants.filter((v) => !hidden.has(v.format)).length - points.length;

  const xs = points.length ? scale(points.map((p) => p.x), M.l, W - M.r) : null;
  const ys = points.length ? scale(points.map((p) => p.y), H - M.b, M.t) : null;
  const maxArea = Math.max(...points.map((p) => p.area), 1e-9);
  const radius = (a: number) => 5 + 11 * Math.sqrt(a / maxArea);
  const fx = xKey === "size" ? fmtBytes : fmtSeconds;
  const fy = yKey === "read" ? fmtBytes : fmtSeconds;
  const xLabel = xKey === "size" ? "File size" : "Write time";
  const yLabel = { warm: "Query time, warm median", cold: "Query time, cold median", read: "Bytes read from storage" }[yKey];
  const areaLabel = xKey === "size" ? "write time" : "file size";
  const fa = xKey === "size" ? fmtSeconds : fmtBytes;

  const best = points.length ? points.reduce((a, b) => (b.y < a.y ? b : a)) : null;
  const labelled = new Set([selected, active, "csv-none", best?.v.id]);
  const tip = points.find((p) => p.v.id === active);

  const onKey = (e: KeyboardEvent, i: number) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (step) {
      e.preventDefault();
      const next = (i + step + points.length) % points.length;
      setFocusIx(next);
      marks.current[next]?.focus();
    } else if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      const next = e.key === "Home" ? 0 : points.length - 1;
      setFocusIx(next);
      marks.current[next]?.focus();
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(points[i].v.id);
    }
  };
  const tabStop = Math.min(focusIx, points.length - 1);

  return (
    <div className="chart">
      <div className="controls">
        <label>Query
          <select value={queryId} onChange={(e) => setQueryId(e.target.value)}>
            {queries.map((q) => <option key={q.id} value={q.id}>{q.id}</option>)}
          </select>
        </label>
        <label>Vertical
          <select value={yKey} onChange={(e) => setYKey(e.target.value as YKey)}>
            <option value="warm">Query time (warm)</option>
            <option value="cold">Query time (cold)</option>
            <option value="read">Bytes read</option>
          </select>
        </label>
        <label>Horizontal
          <select value={xKey} onChange={(e) => setXKey(e.target.value as XKey)}>
            <option value="size">File size</option>
            <option value="write">Write time</option>
          </select>
        </label>
        <div className="legend" role="group" aria-label="Formats shown">
          {FORMATS.map((f) => (
            <button key={f.id} type="button" className="legend-item" aria-pressed={!hidden.has(f.id)}
              onClick={() => setHidden((h) => { const n = new Set(h); if (!n.delete(f.id)) n.add(f.id); return n; })}>
              <svg viewBox="-10 -10 20 20" width="16" height="16" aria-hidden="true" className={`series ${f.id}`}><Shape format={f.id} r={6.5} /></svg>
              {f.label}
            </button>
          ))}
        </div>
      </div>
      <p className="hint">
        {queries.find((q) => q.id === queryId)?.description} Mark area is {areaLabel}. Both axes are logarithmic.
        Arrow keys move between marks; Enter selects one.
      </p>
      {points.length === 0 || !xs || !ys ? (
        <p className="state" role="status">Nothing to plot: every format is hidden, or this query has no measurements.</p>
      ) : (
        <div className="plot">
          <svg viewBox={`0 0 ${W} ${H}`} role="group" aria-label={`${yLabel} against ${xLabel} for ${points.length} variants`}>
            {logTicks(ys.lo, ys.hi).map((t) => (
              <g key={`y${t}`}>
                <line className="grid" x1={M.l} x2={W - M.r} y1={ys.at(t)} y2={ys.at(t)} />
                <text className="tick" x={M.l - 8} y={ys.at(t)} textAnchor="end" dominantBaseline="middle">{fy(t)}</text>
              </g>
            ))}
            {logTicks(xs.lo, xs.hi).map((t) => (
              <g key={`x${t}`}>
                <line className="grid" x1={xs.at(t)} x2={xs.at(t)} y1={M.t} y2={H - M.b} />
                <text className="tick" x={xs.at(t)} y={H - M.b + 16} textAnchor="middle">{fx(t)}</text>
              </g>
            ))}
            <text className="axis" x={(M.l + W - M.r) / 2} y={H - 6} textAnchor="middle">{xLabel} (smaller is better)</text>
            <text className="axis" transform={`translate(13 ${(M.t + H - M.b) / 2}) rotate(-90)`} textAnchor="middle">{yLabel}</text>
            {points.map((p, i) => {
              const r = radius(p.area), cx = xs.at(p.x), cy = ys.at(p.y);
              const on = p.v.id === selected;
              return (
                <g key={p.v.id} ref={(el) => { marks.current[i] = el; }} transform={`translate(${cx} ${cy})`}
                  className={`mark series ${p.v.format}${on ? " selected" : ""}`} tabIndex={i === tabStop ? 0 : -1}
                  role="button" aria-pressed={on}
                  aria-label={`${p.v.id}: ${xLabel.toLowerCase()} ${fx(p.x)}, ${yLabel.toLowerCase()} ${fy(p.y)}, ${areaLabel} ${fa(p.area)}`}
                  onClick={() => { setFocusIx(i); onSelect(p.v.id); }} onKeyDown={(e) => onKey(e, i)}
                  onFocus={() => { setFocusIx(i); setActive(p.v.id); }} onBlur={() => setActive(null)}
                  onPointerEnter={() => setActive(p.v.id)} onPointerLeave={() => setActive(null)}>
                  <circle className="hit" r={r + 7} />
                  {on && <circle className="halo" r={r + 5} />}
                  <Shape format={p.v.format} r={r} />
                  {labelled.has(p.v.id) && (
                    <text className="direct" x={cx > W - 170 ? -r - 6 : r + 6} y={4} textAnchor={cx > W - 170 ? "end" : "start"}>{p.v.id}</text>
                  )}
                </g>
              );
            })}
          </svg>
          {tip && (
            <div className="tooltip" role="presentation"
              style={{ left: `${(xs.at(tip.x) / W) * 100}%`, top: `${(ys.at(tip.y) / H) * 100}%` }}
              data-flip={xs.at(tip.x) > W * 0.6 ? "x" : undefined}>
              <strong>{tip.v.id}</strong>
              <span>{xLabel}: {fx(tip.x)}</span>
              <span>{yLabel}: {fy(tip.y)}</span>
              <span>{areaLabel[0].toUpperCase() + areaLabel.slice(1)}: {fa(tip.area)}</span>
              <span className="muted">read by {tip.v.reader}</span>
            </div>
          )}
        </div>
      )}
      {unplotted > 0 && (
        <p className="hint" role="status">{unplotted} variant{unplotted > 1 ? "s" : ""} not plotted: no successful measurement for this query. See the table.</p>
      )}
    </div>
  );
}
