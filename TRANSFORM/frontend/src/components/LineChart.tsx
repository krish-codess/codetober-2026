import { useMemo } from 'react'
import { extent, line, max, min, scaleLinear, scaleLog, scaleUtc } from 'd3'
import { compact } from '../lib/format'
import { useWidth } from './useWidth'

export interface Series {
  key: string
  label: string
  /** dash pattern doubles the colour so series stay distinguishable without colour */
  dash?: string
  values: { day: string; value: number | null }[]
}

const M = { top: 16, right: 16, bottom: 30, left: 60 }
const toDate = (d: string) => new Date(d + 'T00:00:00Z')
const EPOCH = new Date(0) // fixed fallback domain for an empty series (no wall-clock read during render)

export function LineChart({ series, label, height = 260, log = false, unit = '' }: {
  series: Series[]
  label: string
  height?: number
  log?: boolean
  unit?: string
}) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const g = useMemo(() => {
    const w = Math.max(280, width)
    const innerW = w - M.left - M.right
    const innerH = height - M.top - M.bottom
    const all = series.flatMap((s) => s.values)
    const [x0, x1] = extent(all, (v) => toDate(v.day)) as [Date | undefined, Date | undefined]
    const x = scaleUtc().domain([x0 ?? EPOCH, x1 ?? EPOCH]).range([0, innerW])
    const vals = all.map((v) => v.value).filter((v): v is number => v != null && (!log || v > 0))
    const lo = min(vals) ?? 0
    const hi = max(vals) ?? 1
    const y = log
      ? scaleLog().domain([lo * 0.9, hi * 1.1]).range([innerH, 0]).nice()
      : scaleLinear().domain([Math.min(0, lo), hi * 1.05]).range([innerH, 0]).nice()
    const paths = series.map((s) => ({
      ...s,
      d: line<{ day: string; value: number | null }>()
        .defined((v) => v.value != null && (!log || v.value > 0))
        .x((v) => x(toDate(v.day)))
        .y((v) => y(v.value as number))(s.values),
    }))
    return { w, innerW, innerH, x, y, paths }
  }, [series, width, height, log])

  return (
    <figure className="chart" ref={ref}>
      <svg width={g.w} height={height} role="img" aria-label={label}>
        <g transform={`translate(${M.left},${M.top})`}>
          {g.y.ticks(5).map((t) => (
            <g key={t} transform={`translate(0,${g.y(t)})`}>
              <line x2={g.innerW} className="grid" />
              <text x={-8} dy="0.32em" textAnchor="end" className="tick">
                {compact(t, 2)}
                {unit}
              </text>
            </g>
          ))}
          {g.x.ticks(Math.max(2, Math.floor(g.innerW / 110))).map((t) => (
            <text key={+t} x={g.x(t)} y={g.innerH + 20} textAnchor="middle" className="tick">
              {t.toLocaleDateString(undefined, { month: 'short', year: '2-digit', timeZone: 'UTC' })}
            </text>
          ))}
          {g.paths.map((p, i) =>
            p.d ? <path key={p.key} d={p.d} className={`series s${i % 5}`} strokeDasharray={p.dash} /> : null,
          )}
        </g>
      </svg>
      <figcaption className="legend">
        {series.map((s, i) => (
          <span key={s.key} className="legend-item">
            <svg width="28" height="10" aria-hidden="true">
              <line x1="0" x2="28" y1="5" y2="5" className={`series s${i % 5}`} strokeDasharray={s.dash} />
            </svg>
            {s.label}
          </span>
        ))}
      </figcaption>
    </figure>
  )
}
