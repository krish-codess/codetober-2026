import { useMemo, useState } from 'react'
import { LineChart } from '../components/LineChart'
import { Empty, ErrorPanel, Progress } from '../components/States'
import { qs } from '../lib/api'
import type { Flows, InflationMatrix, World } from '../lib/api'
import { arrow, compact, pct } from '../lib/format'
import { useApi } from '../lib/useApi'

function bucket(r: number): string {
  if (r <= -0.25) return 'b-defl2'
  if (r <= -0.05) return 'b-defl1'
  if (r < 0.05) return 'b-flat'
  if (r < 0.25) return 'b-infl1'
  return 'b-infl2'
}

function rolling(values: { day: string; value: number | null }[], n: number) {
  return values.map((v, i) => {
    const win = values.slice(Math.max(0, i - n + 1), i + 1).map((x) => x.value).filter((x): x is number => x != null)
    return { day: v.day, value: win.length === n ? win.reduce((a, b) => a + b, 0) / n : null }
  })
}

export function InflationPage({ world, server, setServer }: { world: World; server: string; setServer: (s: string) => void }) {
  const [win, setWin] = useState(30)
  const matrix = useApi<InflationMatrix>(`/v1/inflation/matrix${qs({ world: world.world_id, window: win })}`)
  const flowServer = server === 'all' ? world.servers[0]?.server_id : server
  const flows = useApi<Flows>(flowServer ? `/v1/money-flows${qs({ world: world.world_id, server: flowServer })}` : null)
  const cell = useMemo(() => {
    const m = new Map<string, number>()
    for (const c of matrix.data?.cells ?? []) m.set(`${c.server_id}|${c.division_id}`, win >= 365 ? c.rate : c.annualized)
    return m
  }, [matrix.data, win])
  const rows = [{ server_id: 'all', name: 'All servers' }, ...world.servers]
  const cols = [{ division_id: 'all', label: 'All' }, ...world.divisions]
  const flowPts = flows.data?.points ?? []

  return (
    <section aria-labelledby="inf-h">
      <h2 id="inf-h">Inflation by server and category</h2>
      <div className="controls">
        <fieldset className="segmented">
          <legend>Window</legend>
          {[7, 30, 90, 365].map((w) => (
            <label key={w}>
              <input type="radio" name="win" checked={win === w} onChange={() => setWin(w)} />
              {w === 365 ? '1 year' : `${w} days`}
            </label>
          ))}
        </fieldset>
      </div>
      {matrix.loading && <Progress done={0} total={1} what="inflation matrix" />}
      {matrix.error && <ErrorPanel error={matrix.error} onRetry={matrix.reload} />}
      {matrix.data && matrix.data.cells.length === 0 && <Empty>Not enough history yet for a {win}-day window.</Empty>}
      {matrix.data && matrix.data.cells.length > 0 && (
        <div className="table-wrap">
          <table className="heat">
            <caption>
              Latest {win === 365 ? 'year-on-year change' : `${win}-day change, annualised`}. ▲ inflation, ▼ deflation;
              shading repeats the sign and size.
            </caption>
            <thead>
              <tr>
                <th scope="col">Server</th>
                {cols.map((c) => (
                  <th scope="col" key={c.division_id}>
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.server_id}>
                  <th scope="row">{r.name}</th>
                  {cols.map((c) => {
                    const v = cell.get(`${r.server_id}|${c.division_id}`)
                    return (
                      <td key={c.division_id} className={v == null ? 'b-none' : bucket(v)}>
                        {v == null ? '—' : `${arrow(v)} ${pct(v, Math.abs(v) < 0.1 ? 1 : 0)}`}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3>Where the money comes from and goes</h3>
      <div className="controls">
        <label>
          Server
          <select value={flowServer} onChange={(e) => setServer(e.target.value)}>
            {world.servers.map((s) => (
              <option key={s.server_id} value={s.server_id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      {flows.loading && <Progress done={0} total={1} what="currency flows" />}
      {flows.error && <ErrorPanel error={flows.error} onRetry={flows.reload} />}
      {flows.data && flowPts.length > 0 && (
        <>
          <LineChart
            label="Daily currency sinks and faucets (7-day average)"
            series={[
              { key: 'sinks', label: 'Sinks (taxes + fees)', values: rolling(flowPts.map((p) => ({ day: p.day, value: p.sinks })), 7) },
              ...(flowPts.some((p) => p.faucets != null)
                ? [{ key: 'faucets', label: 'Faucets (bounties + rewards)', dash: '6 3', values: rolling(flowPts.map((p) => ({ day: p.day, value: p.faucets ?? null })), 7) }]
                : []),
            ]}
          />
          <ul className="muted methods">
            {Object.entries(flows.data.methods).map(([k, m]) => (
              <li key={k}>
                <strong>{k.replace('_', ' ')}</strong>: {m}
              </li>
            ))}
            {!flowPts.some((p) => p.faucets != null) && (
              <li>No faucet feed exists for this world, so net money supply growth cannot be measured — it is shown as unknown, not zero.</li>
            )}
            <li>
              Latest 7 days: sinks {compact(flowPts.slice(-7).reduce((a, p) => a + p.sinks, 0))} ISK
              {flowPts.some((p) => p.faucets != null) &&
                `, faucets ${compact(flowPts.slice(-7).reduce((a, p) => a + (p.faucets ?? 0), 0))} ISK`}
            </li>
          </ul>
        </>
      )}
    </section>
  )
}
