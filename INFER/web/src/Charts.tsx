import { useState, type KeyboardEvent, type PointerEvent } from "react";
import type { Decile, Run } from "./api";

// Identity is never colour alone: every series also has its own dash pattern, a legend entry with its name,
// a row in the readout under the chart, and a row in the metrics table.
export const SERIES: Record<string, { label: string; color: string; dash?: string }> = {
  x_learner: { label: "X-learner", color: "var(--series-1)" },
  t_learner: { label: "T-learner", color: "var(--series-2)", dash: "9 3" },
  s_learner: { label: "S-learner", color: "var(--series-3)", dash: "2 3" },
  causal_forest: { label: "Causal forest", color: "var(--series-4)", dash: "9 3 2 3" },
  response: { label: "Likely buyers (baseline)", color: "var(--ink-2)", dash: "5 5" },
  random: { label: "Random (baseline)", color: "var(--muted)", dash: "1 4" },
};
export const label = (learner: string): string => SERIES[learner]?.label ?? learner;
export const points = (v: number, digits = 1): string => `${v < 0 ? "−" : v > 0 ? "+" : ""}${Math.abs(v * 100).toFixed(digits)} pts`;

const W = 640;
const H = 320;
const M = { top: 12, right: 16, bottom: 40, left: 52 };
const PW = W - M.left - M.right;
const PH = H - M.top - M.bottom;

function ticks(lo: number, hi: number, count = 5): number[] {
  const step = [1, 2, 2.5, 5, 10].map((m) => m * 10 ** Math.floor(Math.log10((hi - lo) / count))).find((s) => (hi - lo) / s <= count + 1)!;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-12; v += step) out.push(Math.abs(v) < 1e-12 ? 0 : v);
  return out;
}

function Swatch({ learner }: { learner: string }) {
  const s = SERIES[learner];
  return (
    <svg width="28" height="10" aria-hidden="true">
      <line x1="1" x2="27" y1="5" y2="5" stroke={s.color} strokeWidth="2.5" strokeDasharray={s.dash} />
    </svg>
  );
}

export function QiniChart({ runs, kind }: { runs: Run[]; kind: "qini" | "uplift" }) {
  const series = Object.keys(SERIES)
    .map((learner) => runs.find((r) => r.learner === learner && r.curves))
    .filter((r): r is Run => !!r);
  const [at, setAt] = useState(30); // index into the 101-point curves = % of customers targeted
  if (!series.length) return null;

  const values = series.flatMap((r) => r.curves![kind].map((p) => p[1]));
  const lo = Math.min(0, ...values);
  const hi = Math.max(...values) * 1.05;
  const x = (f: number) => M.left + f * PW;
  const y = (v: number) => M.top + (1 - (v - lo) / (hi - lo)) * PH;

  const fromPointer = (e: PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const f = ((e.clientX - box.left) / box.width) * W;
    setAt(Math.max(0, Math.min(100, Math.round(((f - M.left) / PW) * 100))));
  };
  const fromKey = (e: KeyboardEvent<SVGSVGElement>) => {
    const move = { ArrowLeft: -1, ArrowRight: 1, PageDown: -10, PageUp: 10, Home: -100, End: 100 }[e.key];
    if (move === undefined) return;
    e.preventDefault();
    setAt((v) => Math.max(0, Math.min(100, v + move)));
  };
  const name = kind === "qini" ? "Qini curve" : "Cumulative uplift curve";

  return (
    <figure className="chart">
      <ul className="legend" aria-label="Series">
        {series.map((r) => (
          <li key={r.learner}>
            <Swatch learner={r.learner} /> {label(r.learner)}
            {r.is_champion && <strong> ★ champion</strong>}
          </li>
        ))}
      </ul>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        tabIndex={0}
        aria-label={`${name} for ${series.length} models. Use the arrow keys to move along the share of customers targeted; values are read out below.`}
        onPointerMove={fromPointer}
        onPointerDown={fromPointer}
        onKeyDown={fromKey}
      >
        {ticks(lo, hi).map((v) => (
          <g key={v}>
            <line x1={M.left} x2={W - M.right} y1={y(v)} y2={y(v)} className={v === 0 ? "axis" : "grid"} />
            <text x={M.left - 8} y={y(v) + 4} textAnchor="end" className="tick">
              {(v * 100).toFixed(1)}
            </text>
          </g>
        ))}
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <text key={f} x={x(f)} y={H - M.bottom + 18} textAnchor="middle" className="tick">
            {f * 100}%
          </text>
        ))}
        <text x={M.left + PW / 2} y={H - 4} textAnchor="middle" className="axis-title">
          Share of customers targeted, best-ranked first
        </text>
        <text x={12} y={M.top + PH / 2} textAnchor="middle" className="axis-title" transform={`rotate(-90 12 ${M.top + PH / 2})`}>
          Incremental visits (pts)
        </text>
        {series.map((r) => (
          <polyline
            key={r.learner}
            fill="none"
            stroke={SERIES[r.learner].color}
            strokeWidth={r.is_champion ? 3 : 2}
            strokeDasharray={SERIES[r.learner].dash}
            strokeLinejoin="round"
            points={r.curves![kind].map(([f, v]) => `${x(f).toFixed(1)},${y(v).toFixed(1)}`).join(" ")}
          />
        ))}
        <line x1={x(at / 100)} x2={x(at / 100)} y1={M.top} y2={M.top + PH} className="crosshair" />
        {series.map((r) => (
          <circle key={r.learner} cx={x(at / 100)} cy={y(r.curves![kind][at][1])} r="4" fill={SERIES[r.learner].color} stroke="var(--surface)" strokeWidth="2" />
        ))}
      </svg>
      <div className="readout" aria-live="polite">
        <p>
          Targeting the top <strong>{at}%</strong>:
        </p>
        <ul>
          {[...series]
            .sort((a, b) => b.curves![kind][at][1] - a.curves![kind][at][1])
            .map((r) => (
              <li key={r.learner}>
                <Swatch learner={r.learner} /> {label(r.learner)} <strong>{points(r.curves![kind][at][1], 2)}</strong>
              </li>
            ))}
        </ul>
      </div>
      <figcaption>
        Held-out test split. Every curve ends at the same point, the average effect of treating everyone; a better model gets there sooner. The gap
        above the random line is the Qini coefficient.
      </figcaption>
    </figure>
  );
}

export function DecileChart({ deciles }: { deciles: Decile[] }) {
  const [active, setActive] = useState<number | null>(null);
  const lo = Math.min(0, ...deciles.map((d) => d.ci_low));
  const hi = Math.max(0, ...deciles.map((d) => Math.max(d.ci_high, d.mean_predicted)));
  const pad = (hi - lo) * 0.06;
  const y = (v: number) => M.top + (1 - (v - (lo - pad)) / (hi - lo + 2 * pad)) * PH;
  const band = PW / deciles.length;
  const hasTruth = deciles.every((d) => d.true_uplift !== null);
  const d = active === null ? null : deciles[active];

  return (
    <figure className="chart">
      <ul className="legend" aria-label="Marks">
        <li>
          <span className="swatch bar-up" /> observed uplift (bar), helps
        </li>
        <li>
          <span className="swatch bar-down" /> observed uplift (bar), hurts
        </li>
        <li>
          <span className="whisker-key" /> 95% interval
        </li>
        <li>○ predicted by the model</li>
        {hasTruth && <li>◆ true effect (synthetic data)</li>}
      </ul>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Observed uplift by decile of predicted uplift, with 95% intervals. The same numbers are in the table below.">
        {ticks(lo - pad, hi + pad).map((v) => (
          <g key={v}>
            <line x1={M.left} x2={W - M.right} y1={y(v)} y2={y(v)} className={v === 0 ? "axis" : "grid"} />
            <text x={M.left - 8} y={y(v) + 4} textAnchor="end" className="tick">
              {(v * 100).toFixed(0)}
            </text>
          </g>
        ))}
        {deciles.map((dec, i) => {
          const cx = M.left + band * (i + 0.5);
          const top = Math.min(y(dec.observed_uplift), y(0));
          return (
            <g key={dec.decile} onPointerEnter={() => setActive(i)} onPointerLeave={() => setActive(null)}>
              <rect x={M.left + band * i} y={M.top} width={band} height={PH} fill="transparent" />
              <rect
                x={cx - band * 0.3}
                y={top}
                width={band * 0.6}
                height={Math.max(1, Math.abs(y(dec.observed_uplift) - y(0)))}
                rx="3"
                className={dec.observed_uplift < 0 ? "bar-down" : "bar-up"}
                opacity={active === null || active === i ? 1 : 0.55}
              />
              <line x1={cx} x2={cx} y1={y(dec.ci_low)} y2={y(dec.ci_high)} className="whisker" />
              <circle cx={cx} cy={y(dec.mean_predicted)} r="4.5" className="predicted" />
              {dec.true_uplift !== null && <path d={`M${cx} ${y(dec.true_uplift) - 5}l5 5l-5 5l-5-5z`} className="truth" />}
              <text x={cx} y={H - M.bottom + 18} textAnchor="middle" className="tick">
                {dec.decile}
              </text>
            </g>
          );
        })}
        <text x={M.left + PW / 2} y={H - 4} textAnchor="middle" className="axis-title">
          Decile of predicted uplift (1 = model says treat first)
        </text>
        <text x={12} y={M.top + PH / 2} textAnchor="middle" className="axis-title" transform={`rotate(-90 12 ${M.top + PH / 2})`}>
          Uplift (pts)
        </text>
      </svg>
      <p className="readout" aria-hidden="true">
        {d ? (
          <>
            Decile {d.decile}: observed <strong>{points(d.observed_uplift)}</strong> ({points(d.ci_low)} to {points(d.ci_high)}), predicted{" "}
            {points(d.mean_predicted)}
            {d.true_uplift !== null && <>, true {points(d.true_uplift)}</>}
          </>
        ) : (
          "Point at a bar for its numbers, or read the table."
        )}
      </p>
      <details>
        <summary>Decile table</summary>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Decile</th>
                <th scope="col">Predicted</th>
                <th scope="col">Observed</th>
                <th scope="col">95% interval</th>
                {hasTruth && <th scope="col">True</th>}
                <th scope="col">Treated / control</th>
              </tr>
            </thead>
            <tbody>
              {deciles.map((dec) => (
                <tr key={dec.decile}>
                  <th scope="row">{dec.decile}</th>
                  <td>{points(dec.mean_predicted)}</td>
                  <td>{points(dec.observed_uplift)}</td>
                  <td>
                    {points(dec.ci_low)} to {points(dec.ci_high)}
                  </td>
                  {hasTruth && <td>{points(dec.true_uplift!)}</td>}
                  <td>
                    {dec.n_treated.toLocaleString()} / {dec.n_control.toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
