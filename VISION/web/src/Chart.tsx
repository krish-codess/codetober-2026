import { useState } from "react";
import type { Point } from "./api";

export type Metric = "p50_ms" | "p95_ms" | "energy_mj";
export const METRICS: Record<Metric, { label: string; unit: string }> = {
  p50_ms: { label: "Latency p50", unit: "ms" },
  p95_ms: { label: "Latency p95", unit: "ms" },
  energy_mj: { label: "Energy per inference", unit: "mJ" },
};

/** Colour says which network it is; shape says its precision. Neither works alone. */
export function family(p: Point): { key: string; label: string; slot: 1 | 2 | 3 } {
  if (p.arch.startsWith("resnet50-pruned")) return { key: "pruned", label: "ResNet-50, channels pruned", slot: 2 };
  if (p.arch.startsWith("resnet")) return { key: "teacher", label: "ResNet-50 (teacher)", slot: 1 };
  return { key: "student", label: "MobileNetV3-Small (student)", slot: 3 };
}
export const PRECISION: Record<Point["precision"], string> = {
  fp32: "float32",
  int8: "INT8",
  "int8+fp32": "INT8, sensitive layers float32",
};

export const pct = (v: number, digits = 1) => `${(v * 100).toFixed(digits)}%`;
export const num = (v: number) => (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2));

function Mark({ precision, x, y, r, slot, hollow }: { precision: Point["precision"]; x: number; y: number; r: number; slot: number; hollow: boolean }) {
  // 2px ring in the surface colour keeps overlapping marks apart.
  const common = {
    fill: hollow ? "var(--surface)" : `var(--series-${slot})`,
    stroke: hollow ? `var(--series-${slot})` : "var(--surface)",
    strokeWidth: 2,
  };
  if (precision === "fp32") return <circle cx={x} cy={y} r={r} {...common} />;
  if (precision === "int8") return <rect x={x - r} y={y - r} width={2 * r} height={2 * r} {...common} />;
  return <path d={`M${x} ${y - r * 1.3}L${x + r * 1.3} ${y}L${x} ${y + r * 1.3}L${x - r * 1.3} ${y}Z`} {...common} />;
}

export function LegendMark({ precision, slot }: { precision: Point["precision"]; slot: number }) {
  return (
    <svg width="16" height="16" aria-hidden="true">
      <Mark precision={precision} x={8} y={8} r={5} slot={slot} hollow={false} />
    </svg>
  );
}

function logTicks(lo: number, hi: number): number[] {
  const out: number[] = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++)
    for (const m of [1, 2, 5]) {
      const v = m * 10 ** e;
      if (v >= lo && v <= hi) out.push(v);
    }
  return out;
}

const W = 720;
const H = 400;
const M = { top: 20, right: 24, bottom: 46, left: 52 };

export function Scatter({
  points,
  metric,
  budget,
  selected,
  onSelect,
}: {
  points: Point[];
  metric: Metric;
  budget: number | null;
  selected: string | null;
  onSelect: (name: string) => void;
}) {
  const [hover, setHover] = useState<string | null>(null);
  const xs = points.map((p) => p.bench![metric]!);
  const xLo = Math.min(...xs) / 1.35;
  const xHi = Math.max(...xs, metric === "p95_ms" && budget ? budget : 0) * 1.35;
  const yLo = Math.max(0, Math.min(...points.map((p) => p.top1_lo)) - 0.005);
  const yHi = Math.min(1, Math.max(...points.map((p) => p.top1_hi)) + 0.005);
  const x = (v: number) => M.left + ((Math.log10(v) - Math.log10(xLo)) / (Math.log10(xHi) - Math.log10(xLo))) * (W - M.left - M.right);
  const y = (v: number) => H - M.bottom - ((v - yLo) / (yHi - yLo)) * (H - M.top - M.bottom);
  const step = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2].find((s) => (yHi - yLo) / s <= 7) ?? 0.25;
  const yTicks: number[] = [];
  for (let v = Math.ceil(yLo / step) * step; v <= yHi + 1e-9; v += step) yTicks.push(v);
  const active = points.find((p) => p.name === (hover ?? selected));
  const frontier = points.filter((p) => p.pareto).sort((a, b) => a.bench![metric]! - b.bench![metric]!);

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="group" aria-label={`Accuracy against ${METRICS[metric].label.toLowerCase()}, one mark per model. The table below lists the same data.`}>
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={M.left} x2={W - M.right} y1={y(v)} y2={y(v)} className="grid" />
            <text x={M.left - 8} y={y(v)} className="tick" textAnchor="end" dominantBaseline="middle">
              {pct(v, step < 0.01 ? 1 : 0)}
            </text>
          </g>
        ))}
        {logTicks(xLo, xHi).map((v) => (
          <g key={v}>
            <line x1={x(v)} x2={x(v)} y1={M.top} y2={H - M.bottom} className="grid" />
            <text x={x(v)} y={H - M.bottom + 16} className="tick" textAnchor="middle">
              {v}
            </text>
          </g>
        ))}
        <text x={(W + M.left - M.right) / 2} y={H - 8} className="axis" textAnchor="middle">
          {METRICS[metric].label} ({METRICS[metric].unit}, log scale): lower is better
        </text>
        <text x={14} y={(H - M.bottom + M.top) / 2} className="axis" textAnchor="middle" transform={`rotate(-90 14 ${(H - M.bottom + M.top) / 2})`}>
          Top-1 accuracy, 95% interval
        </text>
        {metric === "p95_ms" && budget !== null && (
          <g>
            <line x1={x(budget)} x2={x(budget)} y1={M.top} y2={H - M.bottom} className="budget" />
            <text x={x(budget) + 6} y={M.top + 10} className="tick">
              budget {budget} ms
            </text>
          </g>
        )}
        {metric !== "energy_mj" && frontier.length > 1 && (
          <path className="frontier" d={frontier.map((p, i) => `${i ? "L" : "M"}${x(p.bench![metric]!)} ${y(p.top1)}`).join("")} />
        )}
        {points.map((p) => {
          const px = x(p.bench![metric]!);
          const on = p.name === selected || p.name === hover;
          return (
            <g
              key={p.name}
              role="button"
              tabIndex={0}
              aria-pressed={p.name === selected}
              aria-label={`${p.name}: ${pct(p.top1)} top-1, ${num(p.bench![metric]!)} ${METRICS[metric].unit}, ${PRECISION[p.precision]}${p.gate === "fail" ? ", failed the accuracy gate" : ""}${p.pareto ? ", on the frontier" : ""}`}
              className="mark"
              onClick={() => onSelect(p.name)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect(p.name);
                }
              }}
              onMouseEnter={() => setHover(p.name)}
              onMouseLeave={() => setHover(null)}
              onFocus={() => setHover(p.name)}
              onBlur={() => setHover(null)}
            >
              <line x1={px} x2={px} y1={y(p.top1_lo)} y2={y(p.top1_hi)} className="whisker" />
              <circle cx={px} cy={y(p.top1)} r={16} fill="transparent" />
              {on && <circle cx={px} cy={y(p.top1)} r={11} className="halo" />}
              <Mark precision={p.precision} x={px} y={y(p.top1)} r={6} slot={family(p).slot} hollow={p.gate === "fail"} />
              {(p.pareto || on) && (
                <text x={px + 11} y={y(p.top1) - 9} className="label">
                  {p.name}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {active && (
        <div className="tooltip" role="status">
          <strong>{active.name}</strong>
          <span>
            {pct(active.top1)} top-1 ({pct(active.top1_lo)} to {pct(active.top1_hi)})
          </span>
          <span>
            p50 {num(active.bench!.p50_ms)} ms, p95 {num(active.bench!.p95_ms)} ms, p99 {num(active.bench!.p99_ms)} ms
          </span>
          <span>{active.bench!.energy_mj === null ? "no power measurement" : `${num(active.bench!.energy_mj)} mJ per inference`}</span>
          <span>
            {PRECISION[active.precision]}
            {active.gate === "fail" ? " · failed the accuracy gate" : ""}
          </span>
        </div>
      )}
    </div>
  );
}

export function SensitivityBars({ rows }: { rows: { rank: number; node: string; op_type: string; kl: number; top1_drop: number; kept_float: boolean }[] }) {
  // One series, so one colour and no legend; the kept-float layers carry a text badge.
  const top = rows.slice(0, 12);
  const floor = 1e-5;
  const max = Math.log10(Math.max(...top.map((r) => r.kl), floor * 10) / floor);
  const short = (node: string) => node.replace(/^\/body\//, "").replace(/\/Conv$|\/Gemm$/, "").replace(/features\./g, "f").replace(/block\./g, "b");
  return (
    <ol className="bars">
      {top.map((r) => (
        <li key={r.node} title={r.node}>
          <span className="bar-name">
            {short(r.node)} {r.kept_float && <em className="badge">kept float32</em>}
          </span>
          <span className="bar-track">
            <span className="bar" style={{ width: `${Math.max(1.5, (Math.log10(Math.max(r.kl, floor) / floor) / max) * 100)}%` }} />
          </span>
          <span className="bar-value">
            KL {r.kl < 0.001 ? r.kl.toExponential(1) : r.kl.toFixed(3)} · {r.top1_drop > 0 ? "−" : ""}
            {(Math.abs(r.top1_drop) * 100).toFixed(1)} pt
          </span>
        </li>
      ))}
    </ol>
  );
}
