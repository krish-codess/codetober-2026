import { usePoll, type DurationPoint, type LockWait } from './api'
import { Chart } from './Chart'
import { ErrorNote, n, Stale } from './ui'

export function Analytics() {
  const dur = usePoll<{ points: DurationPoint[]; median_rows_per_second: number }>('/v1/analytics/duration-by-size', 10000)
  const locks = usePoll<{ items: LockWait[] }>('/v1/analytics/lock-waits', 10000)
  const backfilled = dur.data?.points.filter((p) => p.rows > 0) ?? []

  return (
    <>
      <section aria-labelledby="dur-h">
        <h2 id="dur-h">Migration duration by table size</h2>
        <Stale of={dur} />
        {dur.loading && <p className="muted">Loading…</p>}
        {dur.error && !dur.data && <ErrorNote error={dur.error} retry={dur.refresh} />}
        {dur.data && dur.data.points.length === 0 && <p className="muted">No migration has finished expanding yet.</p>}
        {dur.data && dur.data.points.length > 0 && (
          <>
            <p>
              Median backfill throughput: <strong>{n(Math.round(dur.data.median_rows_per_second))} rows per second</strong>, throttle pauses included. New
              plans use it to estimate their backfill.
            </p>
            {backfilled.length > 0 && (
              <div className="charts">
                <Chart
                  kind="dots"
                  title="Backfill time against rows"
                  unit="s"
                  points={backfilled.map((p) => ({ x: p.rows, y: p.backfill_seconds, label: `${p.name} (run ${p.run_id}): ${n(p.rows)} rows` }))}
                  xLabel={(x) => `${n(Math.round(x))} rows`}
                />
              </div>
            )}
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Run</th>
                    <th scope="col">Outcome</th>
                    <th scope="col">Rows</th>
                    <th scope="col">Table size</th>
                    <th scope="col">Expand DDL (s)</th>
                    <th scope="col">Backfill (s)</th>
                    <th scope="col">Rows/s</th>
                    <th scope="col">Dual-write window (s)</th>
                    <th scope="col">Contract (s)</th>
                  </tr>
                </thead>
                <tbody>
                  {dur.data.points.map((p) => (
                    <tr key={p.run_id}>
                      <th scope="row">
                        {p.run_id} · {p.name}
                      </th>
                      <td>{p.state}</td>
                      <td className="num">{n(p.rows)}</td>
                      <td className="num">{(p.bytes / 1048576).toFixed(1)} MB</td>
                      <td className="num">{p.ddl_seconds.toFixed(2)}</td>
                      <td className="num">{p.backfill_seconds.toFixed(1)}</td>
                      <td className="num">{p.rows_per_second ? n(Math.round(p.rows_per_second)) : '—'}</td>
                      <td className="num">{p.dual_write_seconds.toFixed(0)}</td>
                      <td className="num">{p.contract_seconds ? p.contract_seconds.toFixed(2) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <section aria-labelledby="lock-h">
        <h2 id="lock-h">Lock wait time during migration</h2>
        <Stale of={locks} />
        {locks.loading && <p className="muted">Loading…</p>}
        {locks.error && !locks.data && <ErrorNote error={locks.error} retry={locks.refresh} />}
        {locks.data && locks.data.items.length === 0 && <p className="muted">No migration has run yet.</p>}
        {locks.data && locks.data.items.length > 0 && (
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Run</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Longest application block (ms)</th>
                  <th scope="col">95th percentile (ms)</th>
                  <th scope="col">Most sessions blocked</th>
                  <th scope="col">Time with anyone blocked (s)</th>
                  <th scope="col">Migration waited for locks (s)</th>
                  <th scope="col">Samples</th>
                </tr>
              </thead>
              <tbody>
                {locks.data.items.map((w) => (
                  <tr key={w.run_id}>
                    <th scope="row">
                      {w.run_id} · {w.name}
                    </th>
                    <td>{w.state}</td>
                    <td className="num">{n(w.max_wait_ms)}</td>
                    <td className="num">{n(Math.round(w.p95_wait_ms))}</td>
                    <td className="num">{w.max_blocked}</td>
                    <td className="num">{w.blocked_seconds.toFixed(1)}</td>
                    <td className="num">{w.migrator_wait_seconds.toFixed(1)}</td>
                    <td className="num">{n(w.samples)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}
