import { useState } from "react";
import type { Efficiency } from "./api";

// Fixed entity -> colour slot mapping (never by rank): slots 1-4 of the validated palette, in its order.
const SERIES = [
  { key: "mixed", label: "Mixed (default)", slot: 1 },
  { key: "random", label: "Random", slot: 2 },
  { key: "least_confident", label: "Least confident", slot: 3 },
  { key: "entropy_sum", label: "Summed entropy", slot: 4 },
] as const;

const W = 640;
const H = 300;
const M = { top: 16, right: 124, bottom: 40, left: 46 };

export function EfficiencyChart({ data }: { data: Efficiency }) {
  const [hover, setHover] = useState<number | null>(null);
  const sim = data.simulation;
  const live = data.live.filter((p) => p.status !== "rejected");
  const xs = sim ? (sim.curves.random ?? []).map((p) => p.n_labeled) : live.map((p) => p.n_labeled);
  if (xs.length === 0) return <p className="notice">No models have been trained yet, so there is no curve to draw.</p>;

  const all = [
    ...(sim ? Object.values(sim.curves).flatMap((c) => c.map((p) => p.hf1_mean)) : []),
    ...live.map((p) => p.hf1),
    ...(sim ? [sim.full_pool.hf1] : []),
  ];
  const xMax = Math.max(...xs, ...live.map((p) => p.n_labeled));
  const yMin = Math.floor(Math.min(...all) * 10) / 10;
  const yMax = Math.ceil(Math.max(...all) * 20) / 20;
  const x = (v: number) => M.left + (v / xMax) * (W - M.left - M.right);
  const y = (v: number) => H - M.bottom - ((v - yMin) / (yMax - yMin)) * (H - M.top - M.bottom);
  const yTicks = Array.from({ length: 5 }, (_, i) => yMin + ((yMax - yMin) * i) / 4);
  const xTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round((xMax * f) / 100) * 100);
  const hoverN = hover == null ? null : (xs[hover] ?? null);

  // Direct labels at the line ends, nudged apart so they never overlap.
  const ends = sim
    ? SERIES.map((s) => ({ ...s, v: sim.curves[s.key]?.at(-1)?.hf1_mean ?? 0 }))
        .sort((a, b) => b.v - a.v)
        .map((s, i, arr) => ({ ...s, ly: Math.max(y(s.v), i === 0 ? 0 : y(arr[0]?.v ?? 0) + i * 14) }))
    : [];

  return (
    <figure className="viz-root chart">
      <figcaption>
        Held-out hierarchical F1 by number of labelled items
        {sim && (
          <span className="muted">
            {" "}
            — offline simulation on the real corpus, mean of 3 seeds; the summed-entropy line is the strategy that
            failed (see README)
          </span>
        )}
      </figcaption>
      {sim && (
        <ul className="legend" aria-label="Series">
          {SERIES.map((s) => (
            <li key={s.key}>
              <span className={`swatch s${s.slot}`} aria-hidden="true" />
              {s.label}
            </li>
          ))}
          {live.length > 0 && (
            <li>
              <span className="swatch ring" aria-hidden="true" />
              This deployment
            </li>
          )}
        </ul>
      )}
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Line chart of held-out hierarchical F1 against number of labelled items. The same data is in the table below."
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          const px = ((e.clientX - box.left) / box.width) * W;
          let best = 0;
          xs.forEach((v, i) => {
            if (Math.abs(x(v) - px) < Math.abs(x(xs[best] ?? 0) - px)) best = i;
          });
          setHover(best);
        }}
      >
        {yTicks.map((t) => (
          <g key={t}>
            <line className="grid" x1={M.left} x2={W - M.right} y1={y(t)} y2={y(t)} />
            <text className="tick" x={M.left - 8} y={y(t) + 4} textAnchor="end">
              {t.toFixed(2)}
            </text>
          </g>
        ))}
        {xTicks.map((t) => (
          <text key={t} className="tick" x={x(t)} y={H - M.bottom + 18} textAnchor="middle">
            {t.toLocaleString()}
          </text>
        ))}
        <text className="tick" x={(M.left + W - M.right) / 2} y={H - 4} textAnchor="middle">
          labelled items
        </text>
        {sim && (
          <g>
            <line
              className="reference"
              x1={M.left}
              x2={W - M.right}
              y1={y(sim.full_pool.hf1)}
              y2={y(sim.full_pool.hf1)}
            />
            <text className="tick" x={W - M.right + 6} y={y(sim.full_pool.hf1) + 4}>
              all {sim.full_pool.n_labeled.toLocaleString()} labelled
            </text>
          </g>
        )}
        {sim &&
          SERIES.map((s) => (
            <polyline
              key={s.key}
              className={`line s${s.slot}`}
              points={(sim.curves[s.key] ?? []).map((p) => `${x(p.n_labeled)},${y(p.hf1_mean)}`).join(" ")}
            />
          ))}
        {ends.map((s) => (
          <text key={s.key} className="tick strong" x={W - M.right + 6} y={s.ly + 4}>
            {s.label}
          </text>
        ))}
        {live.map((p) => (
          <circle key={p.model_version} className="live" cx={x(p.n_labeled)} cy={y(p.hf1)} r={4.5}>
            <title>{`This deployment, model v${p.model_version}: ${p.n_labeled} labels, hF1 ${p.hf1.toFixed(3)}`}</title>
          </circle>
        ))}
        {hoverN != null && sim && (
          <g pointerEvents="none">
            <line className="crosshair" x1={x(hoverN)} x2={x(hoverN)} y1={M.top} y2={H - M.bottom} />
            {SERIES.map((s) => {
              const p = sim.curves[s.key]?.[hover ?? 0];
              return p ? (
                <circle key={s.key} className={`dot s${s.slot}`} cx={x(p.n_labeled)} cy={y(p.hf1_mean)} r={4} />
              ) : null;
            })}
          </g>
        )}
      </svg>
      <p className="readout" aria-hidden="true">
        {hoverN != null && sim
          ? `${hoverN.toLocaleString()} labels — ${SERIES.map((s) => `${s.label}: ${(sim.curves[s.key]?.[hover ?? 0]?.hf1_mean ?? 0).toFixed(3)}`).join(" · ")}`
          : "Hover or use the table for exact values."}
      </p>
      <details>
        <summary>Show as table</summary>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Labels</th>
                {sim &&
                  SERIES.map((s) => (
                    <th key={s.key} scope="col">
                      {s.label}
                    </th>
                  ))}
                <th scope="col">This deployment</th>
              </tr>
            </thead>
            <tbody>
              {[...new Set([...xs, ...live.map((p) => p.n_labeled)])]
                .sort((a, b) => a - b)
                .map((n) => (
                  <tr key={n}>
                    <th scope="row">{n.toLocaleString()}</th>
                    {sim &&
                      SERIES.map((s) => {
                        const p = sim.curves[s.key]?.find((q) => q.n_labeled === n);
                        return <td key={s.key}>{p ? `${p.hf1_mean.toFixed(3)} ± ${p.hf1_sd.toFixed(3)}` : "–"}</td>;
                      })}
                    <td>{live.find((p) => p.n_labeled === n)?.hf1.toFixed(3) ?? "–"}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
