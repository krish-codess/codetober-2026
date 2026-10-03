import { useMemo, useRef, useState } from 'react'
import { extent, line, max, min, scaleLinear, scaleUtc } from 'd3'
import type { IndexPoint, Patch, Shock } from '../lib/api'
import { compact, day as fmtDay, pct } from '../lib/format'
import { useWidth } from './useWidth'

export interface IndexChartProps {
  points: IndexPoint[]
  patches: Patch[]
  shocks: Shock[]
  label: string
  height?: number
  focusDay?: string | null
  onFocusDay?: (day: string | null) => void
}

const M = { top: 28, right: 16, bottom: 46, left: 52 }
const toDate = (d: string) => new Date(d + 'T00:00:00Z')
const EPOCH = new Date(0) // fixed fallback domain for an empty series (no wall-clock read during render)

/**
 * React owns the DOM, d3 does the maths (scales, path geometry). That keeps the chart testable and
 * accessible: it is one focusable element with a keyboard model (←/→ day, PgUp/PgDn 30 days,
 * Home/End) and an aria-live readout, and every visual encoding has a non-colour twin
 * (dash pattern for partial coverage, ▲/▼ glyphs for shock direction, filled vs hollow for persistence).
 */
export function IndexChart({ points, patches, shocks, label, height = 360, focusDay, onFocusDay }: IndexChartProps) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hoverPatch, setHoverPatch] = useState<Patch | null>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const [internalFocus, setInternalFocus] = useState<string | null>(null)
  const focused = focusDay !== undefined ? focusDay : internalFocus
  const setFocus = (d: string | null) => (onFocusDay ? onFocusDay(d) : setInternalFocus(d))

  const geo = useMemo(() => {
    const w = Math.max(280, width)
    const innerW = w - M.left - M.right
    const innerH = height - M.top - M.bottom
    const days = points.map((p) => toDate(p.day))
    const [x0, x1] = extent(days) as [Date | undefined, Date | undefined]
    const x = scaleUtc()
      .domain([x0 ?? EPOCH, x1 ?? EPOCH])
      .range([0, innerW])
    const values = points.map((p) => p.value).filter((v): v is number => v != null)
    const lo = min(values) ?? 90
    const hi = max(values) ?? 110
    const pad = Math.max((hi - lo) * 0.08, 1)
    const y = scaleLinear().domain([lo - pad, hi + pad]).nice().range([innerH, 0])
    const path = line<IndexPoint>()
      .defined((p) => p.value != null)
      .x((p) => x(toDate(p.day)))
      .y((p) => y(p.value as number))(points)
    // partial-coverage stretches are re-drawn dashed on top of the solid line
    const partial = line<IndexPoint>()
      .defined((p) => p.value != null && p.status === 'partial')
      .x((p) => x(toDate(p.day)))
      .y((p) => y(p.value as number))(points)
    const inRange = (d: Date) => x0 != null && x1 != null && d >= x0 && d <= x1
    const visiblePatches = patches.filter((p) => inRange(new Date(p.released_at)))
    return { w, innerW, innerH, x, y, path, partial, visiblePatches, inRange }
  }, [points, patches, width, height])

  const byDay = useMemo(() => new Map(points.map((p, i) => [p.day, i])), [points])
  const shockByDay = useMemo(() => new Map(shocks.map((s) => [s.day, s])), [shocks])
  const fi = focused != null ? byDay.get(focused) : undefined
  const fp = fi != null ? points[fi] : undefined
  const fShock = fp ? shockByDay.get(fp.day) : undefined

  function move(delta: number | 'home' | 'end') {
    if (!points.length) return
    const cur = fi ?? points.length - 1
    const next = delta === 'home' ? 0 : delta === 'end' ? points.length - 1 : Math.min(points.length - 1, Math.max(0, cur + delta))
    setFocus(points[next].day)
  }

  function onKeyDown(e: React.KeyboardEvent) {
    const map: Record<string, number | 'home' | 'end'> = {
      ArrowLeft: -1, ArrowRight: 1, PageUp: -30, PageDown: 30, Home: 'home', End: 'end',
    }
    if (e.key in map) {
      e.preventDefault()
      move(map[e.key])
    } else if (e.key === 'Escape') {
      setFocus(null)
    }
  }

  function onPointer(e: React.PointerEvent<SVGSVGElement>) {
    const box = svgRef.current?.getBoundingClientRect()
    if (!box || !points.length) return
    const t = geo.x.invert(e.clientX - box.left - M.left)
    const iso = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate() + (t.getUTCHours() >= 12 ? 1 : 0)))
      .toISOString()
      .slice(0, 10)
    if (byDay.has(iso)) setFocus(iso)
  }

  const ticks = geo.x.ticks(Math.max(2, Math.floor(geo.innerW / 110)))
  const yTicks = geo.y.ticks(6)

  return (
    <div className="chart" ref={ref}>
      <svg
        ref={svgRef}
        width={geo.w}
        height={height}
        role="application"
        aria-roledescription="interactive chart"
        aria-label={`${label}. Use left and right arrow keys to move between days, Page Up and Page Down to jump 30 days.`}
        tabIndex={0}
        onKeyDown={onKeyDown}
        onPointerMove={onPointer}
        onPointerLeave={() => setHoverPatch(null)}
        className="chart-svg"
      >
        <defs>
          <pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="6" className="hatch-line" strokeWidth="3" />
          </pattern>
        </defs>
        <g transform={`translate(${M.left},${M.top})`}>
          {yTicks.map((t) => (
            <g key={t} transform={`translate(0,${geo.y(t)})`}>
              <line x2={geo.innerW} className="grid" />
              <text x={-8} dy="0.32em" textAnchor="end" className="tick">
                {t}
              </text>
            </g>
          ))}
          <line y1={geo.y(100)} y2={geo.y(100)} x2={geo.innerW} className="ref-line" />
          {ticks.map((t) => (
            <text key={+t} x={geo.x(t)} y={geo.innerH + 18} textAnchor="middle" className="tick">
              {t.toLocaleDateString(undefined, { month: 'short', year: '2-digit', timeZone: 'UTC' })}
            </text>
          ))}
          {/* coverage strip: hatched where partial, solid where no value could be published */}
          {points.map((p) =>
            p.status === 'ok' ? null : (
              <rect
                key={p.day}
                x={geo.x(toDate(p.day)) - 1.5}
                y={geo.innerH + 26}
                width={3}
                height={8}
                className={p.status === 'partial' ? 'cov-partial' : 'cov-missing'}
                fill={p.status === 'partial' ? 'url(#hatch)' : undefined}
              />
            ),
          )}
          {geo.visiblePatches.map((p) => {
            const px = geo.x(new Date(p.released_at))
            return (
              <g key={p.patch_id} className={p.is_major ? 'patch major' : 'patch'}>
                <line x1={px} x2={px} y1={p.is_major ? -6 : geo.innerH - 10} y2={geo.innerH} />
                <rect
                  x={px - 4}
                  y={p.is_major ? -12 : geo.innerH - 14}
                  width={8}
                  height={geo.innerH + 14}
                  className="patch-hit"
                  onPointerEnter={() => setHoverPatch(p)}
                >
                  <title>{`${p.title} — ${fmtDay(p.released_at.slice(0, 10))}`}</title>
                </rect>
                {p.is_major && (
                  <text x={px + 3} y={-14} className="patch-label">
                    {p.version ? `v${p.version}` : p.title.slice(0, 18)}
                  </text>
                )}
              </g>
            )
          })}
          {geo.path && <path d={geo.path} className="series" />}
          {geo.partial && <path d={geo.partial} className="series-partial" />}
          {shocks.map((s) => {
            const p = points[byDay.get(s.day) ?? -1]
            if (!p || p.value == null) return null
            const cx = geo.x(toDate(s.day))
            const cy = geo.y(p.value)
            const up = s.direction === 'up'
            const persistent = (s.persistence ?? 0) >= 0.5
            const d = up ? `M${cx},${cy - 14} l6,10 h-12 z` : `M${cx},${cy + 14} l6,-10 h-12 z`
            return (
              <path key={s.day} d={d} className={`shock ${up ? 'up' : 'down'} ${persistent ? 'persistent' : 'transient'}`}>
                <title>{`${up ? 'Up' : 'Down'} shock ${pct(Math.expm1(s.log_change))} on ${fmtDay(s.day)}`}</title>
              </path>
            )
          })}
          {fp && fp.value != null && (
            <g className="crosshair">
              <line x1={geo.x(toDate(fp.day))} x2={geo.x(toDate(fp.day))} y1={0} y2={geo.innerH} />
              <circle cx={geo.x(toDate(fp.day))} cy={geo.y(fp.value)} r={4.5} />
            </g>
          )}
        </g>
      </svg>
      <div className="readout" aria-live="polite" data-testid="readout">
        {fp ? (
          <>
            <strong>{fmtDay(fp.day)}</strong>: {fp.value != null ? compact(fp.value, 4) : 'no value'}{' '}
            <span className={`badge ${fp.status}`}>{fp.status}</span> coverage {Math.round(fp.coverage * 100)}%
            {fp.revised && <span className="badge revised"> revised (v{fp.vintage})</span>}
            {fShock && (
              <span>
                {' '}
                · {fShock.direction === 'up' ? '▲' : '▼'} shock {pct(Math.expm1(fShock.log_change))}
                {fShock.attributions[0] ? ` — likely ${fShock.attributions[0].title}` : ' — unattributed'}
              </span>
            )}
          </>
        ) : hoverPatch ? (
          <>
            <strong>{hoverPatch.title}</strong> · {fmtDay(hoverPatch.released_at.slice(0, 10))}
            {hoverPatch.impact && <> · 7-day move {pct(Math.expm1(hoverPatch.impact.log_change))}</>}
          </>
        ) : (
          <span className="muted">Focus the chart and use the arrow keys, or hover, to inspect a day.</span>
        )}
      </div>
    </div>
  )
}
