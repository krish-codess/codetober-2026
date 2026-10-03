import { useEffect, useMemo, useState } from 'react'
import { IndexChart } from '../components/IndexChart'
import { Empty, ErrorPanel, PartialBanner, Progress, StaleBanner } from '../components/States'
import { getAllPages, qs } from '../lib/api'
import type { ApiError } from '../lib/api'
import type { IndexResponse, InflationResponse, Patch, ShocksResponse, World } from '../lib/api'
import { addDays, arrow, day, pct } from '../lib/format'
import { toApiError, useApi } from '../lib/useApi'

const RANGES = [
  { id: '90d', label: '3 months', days: 90 },
  { id: '180d', label: '6 months', days: 180 },
  { id: '365d', label: '1 year', days: 365 },
  { id: 'all', label: 'All', days: 0 },
]

function usePatches(world: string, from: string | undefined, to: string | undefined) {
  const [nonce, setNonce] = useState(0)
  const key = `${world}|${from}|${to}|${nonce}`
  const [settled, setSettled] = useState<{ key: string; data?: Patch[]; error?: ApiError } | null>(null)
  const [pages, setPages] = useState({ key: '', n: 0 })
  useEffect(() => {
    let live = true
    getAllPages<Patch>(
      (cursor) => `/v1/patches${qs({ world, from, to, limit: 200, cursor })}`,
      (n) => live && setPages({ key, n }),
    )
      .then((data) => live && setSettled({ key, data }))
      .catch((e: unknown) => live && setSettled({ key, error: toApiError(e) }))
    return () => {
      live = false
    }
  }, [world, from, to, key])
  const current = settled?.key === key
  return {
    data: settled?.data,
    error: current ? settled?.error : undefined,
    loading: !current,
    pages: pages.key === key ? pages.n : 0,
    reload: () => setNonce((n) => n + 1),
  }
}

export function IndexPage({ world, server, setServer }: { world: World; server: string; setServer: (s: string) => void }) {
  const [division, setDivision] = useState('all')
  const [range, setRange] = useState('365d')
  const [focusDay, setFocusDay] = useState<string | null>(null)
  const last = world.freshness.last_day ?? undefined
  const days = RANGES.find((r) => r.id === range)?.days ?? 0
  const from = last && days ? addDays(last, -days) : undefined
  const params = { world: world.world_id, server, division, from, to: last }

  const index = useApi<IndexResponse>(`/v1/index${qs(params)}`)
  const shocks = useApi<ShocksResponse>(`/v1/shocks${qs(params)}`)
  const infl = useApi<InflationResponse>(`/v1/inflation${qs({ ...params, from: undefined, window: 30 })}`)
  const yoy = useApi<InflationResponse>(`/v1/inflation${qs({ ...params, from: undefined, window: 365 })}`)
  const patches = usePatches(world.world_id, from, last)

  const pending = [index, shocks, infl, yoy].filter((x) => x.loading).length + (patches.loading ? 1 : 0)
  const error = index.error ?? shocks.error ?? patches.error
  const points = useMemo(() => index.data?.points ?? [], [index.data])
  const counts = useMemo(
    () => ({
      partial: points.filter((p) => p.status === 'partial').length,
      missing: points.filter((p) => p.status === 'insufficient').length,
      revised: points.filter((p) => p.revised).length,
    }),
    [points],
  )
  const first = points.find((p) => p.value != null)
  const lastP = [...points].reverse().find((p) => p.value != null)
  const change = first && lastP ? (lastP.value as number) / (first.value as number) - 1 : null
  const latest30 = infl.data?.points.at(-1)
  const latest365 = yoy.data?.points.at(-1)
  const seriesLabel = `${server === 'all' ? 'All servers' : world.servers.find((s) => s.server_id === server)?.name}, ${
    division === 'all' ? 'all divisions' : world.divisions.find((d) => d.division_id === division)?.label
  }`
  const majorPatches = (patches.data ?? []).filter((p) => p.is_major || (p.impact && Math.abs(p.impact.robust_z) > 4))

  return (
    <section aria-labelledby="index-h">
      <h2 id="index-h">Price index</h2>
      <div className="controls">
        <label>
          Server
          <select value={server} onChange={(e) => setServer(e.target.value)}>
            <option value="all">All servers (cross-server)</option>
            {world.servers.map((s) => (
              <option key={s.server_id} value={s.server_id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Division
          <select value={division} onChange={(e) => setDivision(e.target.value)}>
            <option value="all">All divisions (headline)</option>
            {world.divisions.map((d) => (
              <option key={d.division_id} value={d.division_id}>
                {d.label}
              </option>
            ))}
          </select>
        </label>
        <fieldset className="segmented">
          <legend>Range</legend>
          {RANGES.map((r) => (
            <label key={r.id}>
              <input type="radio" name="range" value={r.id} checked={range === r.id} onChange={() => setRange(r.id)} />
              {r.label}
            </label>
          ))}
        </fieldset>
      </div>

      <StaleBanner freshness={index.data?.freshness ?? world.freshness} />
      {pending > 0 && <Progress done={5 - pending} total={5} what="index, shocks, inflation and patch notes" />}
      {error && (
        <ErrorPanel
          error={error}
          onRetry={() => {
            index.reload()
            shocks.reload()
            patches.reload()
          }}
        />
      )}

      {!index.loading && !error && points.length === 0 && (
        <Empty>
          No index values are published for {seriesLabel} in this range. Either the basket for this period is not
          frozen yet, or the series has no eligible items. Try a longer range or “All divisions”.
        </Empty>
      )}

      {points.length > 0 && (
        <>
          <PartialBanner {...counts} />
          <div className="stats" role="group" aria-label="Summary">
            <div className="stat">
              <span className="stat-label">Index level</span>
              <span className="stat-value">{lastP?.value?.toFixed(1) ?? '—'}</span>
              <span className="muted">{lastP ? day(lastP.day) : ''} · reference 100</span>
            </div>
            <div className="stat">
              <span className="stat-label">Change over range</span>
              <span className="stat-value">
                {arrow(change)} {pct(change)}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">30-day inflation (annualised)</span>
              <span className="stat-value">
                {arrow(latest30?.annualized)} {pct(latest30?.annualized)}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">Year-on-year</span>
              <span className="stat-value">
                {arrow(latest365?.rate)} {pct(latest365?.rate)}
              </span>
            </div>
          </div>
          <IndexChart
            points={points}
            patches={patches.data ?? []}
            shocks={shocks.data?.shocks ?? []}
            label={`Price index, ${seriesLabel}`}
            focusDay={focusDay}
            onFocusDay={setFocusDay}
          />
          <p className="legend-note muted">
            Solid line: full coverage · dashed: partial coverage · gap: not enough data to publish (never interpolated).
            ▲▼ shocks — filled: the move persisted, hollow: it reverted within 3 days. Ticks on the axis are patches;
            labelled lines are major releases.
          </p>
        </>
      )}

      {(shocks.data?.shocks.length ?? 0) > 0 && (
        <details open className="panel">
          <summary>
            <h3>Shocks and their likely cause ({shocks.data?.shocks.length})</h3>
          </summary>
          <div className="table-wrap">
            <table>
              <caption className="sr-only">Detected price shocks for {seriesLabel}</caption>
              <thead>
                <tr>
                  <th scope="col">Day</th>
                  <th scope="col">Move</th>
                  <th scope="col">Persisted</th>
                  <th scope="col">Most likely patch</th>
                </tr>
              </thead>
              <tbody>
                {[...(shocks.data?.shocks ?? [])]
                  .sort((a, b) => Math.abs(b.log_change) - Math.abs(a.log_change))
                  .slice(0, 25)
                  .map((s) => (
                    <tr key={s.day}>
                      <td>
                        <button type="button" className="link" onClick={() => setFocusDay(s.day)}>
                          {day(s.day)}
                        </button>
                      </td>
                      <td>
                        {s.direction === 'up' ? '▲' : '▼'} {pct(Math.expm1(s.log_change))}
                      </td>
                      <td>{s.persistence == null ? 'not yet known' : s.persistence >= 0.5 ? 'yes' : 'no (reverted)'}</td>
                      <td>
                        {s.attributions[0] ? (
                          <>
                            {s.attributions[0].title}{' '}
                            <span className="muted">
                              ({Math.round(s.attributions[0].lag_hours)} h earlier, relevance{' '}
                              {s.attributions[0].relevance.toFixed(2)})
                            </span>
                          </>
                        ) : (
                          <span className="muted">Unattributed — no patch in the 3 days before</span>
                        )}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {majorPatches.length > 0 && (
        <details className="panel">
          <summary>
            <h3>Major patches in range ({majorPatches.length})</h3>
          </summary>
          <ul className="patch-list">
            {majorPatches.map((p) => (
              <li key={p.patch_id}>
                <strong>{p.title}</strong> · {day(p.released_at.slice(0, 10))}
                {p.impact && (
                  <>
                    {' '}
                    · headline 7-day move {arrow(p.impact.log_change)} {pct(Math.expm1(p.impact.log_change))} (z{' '}
                    {p.impact.robust_z.toFixed(1)})
                  </>
                )}
                {p.tags.length > 0 && <span className="muted"> · touches {p.tags.join(', ')}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}
