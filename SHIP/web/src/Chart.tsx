import { useState, type KeyboardEvent, type PointerEvent } from 'react'

export interface Point {
  x: number
  y: number
  label?: string
}

const W = 360
const H = 150
const PAD = { l: 44, r: 12, t: 10, b: 24 }

/** Round a maximum up to 1, 2 or 5 times a power of ten, so the axis ends on a readable number. */
export function niceMax(v: number): number {
  if (v <= 0) return 1
  const p = 10 ** Math.floor(Math.log10(v))
  return [1, 2, 5, 10].map((m) => m * p).find((m) => m >= v)!
}

/** Keep at most `n` points, each the highest of its bucket: a spike must survive downsampling. */
export function downsample(points: Point[], n: number): Point[] {
  if (points.length <= n) return points
  const size = Math.ceil(points.length / n)
  const out: Point[] = []
  for (let i = 0; i < points.length; i += size) {
    out.push(points.slice(i, i + size).reduce((a, b) => (b.y > a.y ? b : a)))
  }
  return out
}

const fmt = (v: number) => (Math.abs(v) >= 10000 ? Intl.NumberFormat('en', { notation: 'compact' }).format(v) : String(Math.round(v * 10) / 10))

interface Props {
  title: string
  unit: string
  points: Point[]
  /** 'line' joins points in x order (a time series); 'dots' plots them alone (one mark per run). */
  kind?: 'line' | 'dots'
  xLabel: (x: number) => string
  /** A horizontal reference line, e.g. the abort threshold. */
  limit?: { y: number; label: string }
}

/**
 * One measure, one axis, one series: the title names it, so there is no legend.
 * Hover or focus and use the arrow keys to read exact values.
 */
export function Chart({ title, unit, points, kind = 'line', xLabel, limit }: Props) {
  const [at, setAt] = useState<number | null>(null)
  const pts = kind === 'line' ? downsample(points, 300) : points
  if (pts.length === 0) {
    return (
      <figure className="chart">
        <figcaption>{title}</figcaption>
        <p className="muted chart-empty">No samples yet.</p>
      </figure>
    )
  }
  const xs = pts.map((p) => p.x)
  const x0 = kind === 'dots' ? 0 : Math.min(...xs)
  const x1 = Math.max(...xs, x0 + 1)
  const yMax = niceMax(Math.max(...pts.map((p) => p.y), limit?.y ?? 0))
  const sx = (x: number) => PAD.l + ((x - x0) / (x1 - x0)) * (W - PAD.l - PAD.r)
  const sy = (y: number) => H - PAD.b - (y / yMax) * (H - PAD.t - PAD.b)
  const peak = pts.reduce((a, b) => (b.y > a.y ? b : a))
  const cur = at === null ? null : pts[Math.min(at, pts.length - 1)]!

  const nearest = (e: PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    const x = ((e.clientX - box.left) / box.width) * W
    let best = 0
    pts.forEach((p, i) => {
      if (Math.abs(sx(p.x) - x) < Math.abs(sx(pts[best]!.x) - x)) best = i
    })
    setAt(best)
  }
  const onKey = (e: KeyboardEvent) => {
    const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0
    if (!step) return
    e.preventDefault()
    setAt((i) => Math.max(0, Math.min(pts.length - 1, (i ?? (step > 0 ? -1 : pts.length)) + step)))
  }

  return (
    <figure className="chart">
      <figcaption>
        {title} <span className="muted">({unit})</span>
      </figcaption>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        tabIndex={0}
        aria-label={`${title}: ${pts.length} points, highest ${fmt(peak.y)} ${unit}. Use the left and right arrow keys to read values.`}
        onPointerMove={nearest}
        onPointerLeave={() => setAt(null)}
        onKeyDown={onKey}
        onBlur={() => setAt(null)}
      >
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line className={f === 0 ? 'axis' : 'grid'} x1={PAD.l} x2={W - PAD.r} y1={sy(yMax * f)} y2={sy(yMax * f)} />
            <text className="tick" x={PAD.l - 6} y={sy(yMax * f) + 4} textAnchor="end">
              {fmt(yMax * f)}
            </text>
          </g>
        ))}
        <text className="tick" x={PAD.l} y={H - 6}>
          {xLabel(x0)}
        </text>
        <text className="tick" x={W - PAD.r} y={H - 6} textAnchor="end">
          {xLabel(x1)}
        </text>
        {limit && (
          <g>
            <line className="limit" x1={PAD.l} x2={W - PAD.r} y1={sy(limit.y)} y2={sy(limit.y)} />
            <text className="tick" x={W - PAD.r} y={sy(limit.y) - 4} textAnchor="end">
              {limit.label}
            </text>
          </g>
        )}
        {kind === 'line' ? (
          <polyline className="series" points={pts.map((p) => `${sx(p.x)},${sy(p.y)}`).join(' ')} />
        ) : (
          pts.map((p, i) => <circle key={i} className="dot" cx={sx(p.x)} cy={sy(p.y)} r={5} />)
        )}
        {cur && (
          <g>
            <line className="crosshair" x1={sx(cur.x)} x2={sx(cur.x)} y1={PAD.t} y2={H - PAD.b} />
            <circle className="dot" cx={sx(cur.x)} cy={sy(cur.y)} r={5} />
          </g>
        )}
      </svg>
      {/* The readout sits under the plot rather than floating over it: it never covers the data and works by keyboard. */}
      <p className="readout" aria-live="polite">
        {cur ? (
          <>
            <strong>
              {fmt(cur.y)} {unit}
            </strong>{' '}
            <span className="muted">{cur.label ?? xLabel(cur.x)}</span>
          </>
        ) : (
          <span className="muted">
            Highest {fmt(peak.y)} {unit}
          </span>
        )}
      </p>
    </figure>
  )
}
