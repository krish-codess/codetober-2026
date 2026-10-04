import { useEffect, useState } from 'react'
import { Empty, ErrorPanel, Progress } from '../components/States'
import { ApiError, getJSON, qs } from '../lib/api'
import type { ManipulationPage, World } from '../lib/api'
import { compact, day } from '../lib/format'

type Item = ManipulationPage['items'][number]
const KINDS: Record<string, string> = {
  extreme_listing: 'Extreme listing (rejected by the estimator)',
  rejected_price: 'Rejected daily price',
  thin_market_spike: 'Thin-market spike (flagged, kept)',
}

export function IntegrityPage({ world, server, setServer }: { world: World; server: string; setServer: (s: string) => void }) {
  const active = server === 'all' ? world.servers.at(-1)?.server_id ?? '' : server
  const [kind, setKind] = useState('')
  const [items, setItems] = useState<Item[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [state, setState] = useState<{ loading: boolean; error?: ApiError }>({ loading: true })

  async function load(next: string | null, reset: boolean) {
    setState({ loading: true })
    try {
      const page = await getJSON<ManipulationPage>(
        `/v1/manipulation${qs({ world: world.world_id, server: active, kind, limit: 25, cursor: next })}`,
      )
      setItems((cur) => (reset ? page.items : [...cur, ...page.items]))
      setCursor(page.page.next_cursor ?? null)
      setState({ loading: false })
    } catch (e) {
      setState({ loading: false, error: e as ApiError })
    }
  }

  useEffect(() => {
    void load(null, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [world.world_id, active, kind])

  return (
    <section aria-labelledby="int-h">
      <h2 id="int-h">Market integrity</h2>
      <p className="lede">
        Every listing or trade day the estimators refused to trust, newest first. Thin markets (few listings) are where
        manipulation works, so they are marked.
      </p>
      <div className="controls">
        <label>
          Server
          <select value={active} onChange={(e) => setServer(e.target.value)}>
            {world.servers.map((s) => (
              <option key={s.server_id} value={s.server_id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Kind
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">All kinds</option>
            {Object.entries(KINDS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
      </div>
      {state.error && <ErrorPanel error={state.error} onRetry={() => void load(cursor, false)} />}
      {!state.loading && !state.error && items.length === 0 && <Empty>No events of this kind on this server.</Empty>}
      {items.length > 0 && (
        <div className="table-wrap">
          <table className="cards">
            <caption className="sr-only">Manipulation and thin-market events</caption>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col">Item</th>
                <th scope="col">Event</th>
                <th scope="col">Severity</th>
                <th scope="col">Market</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {items.map((e) => (
                <tr key={`${e.day}-${e.item_id}-${e.kind}`}>
                  <td data-label="Day">{day(e.day)}</td>
                  <td data-label="Item">{e.item_name}</td>
                  <td data-label="Event">{KINDS[e.kind] ?? e.kind}</td>
                  <td data-label="Severity">{e.severity.toFixed(1)}</td>
                  <td data-label="Market">{e.thin ? <span className="badge partial">thin · {e.n_obs}</span> : `${e.n_obs} obs`}</td>
                  <td data-label="Detail" className="detail">{describe(e)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {state.loading && <Progress done={0} total={1} what="events" />}
      {cursor && !state.loading && (
        <button type="button" onClick={() => void load(cursor, false)}>
          Load more
        </button>
      )}
    </section>
  )
}

function describe(e: Item): string {
  const d = e.detail as Record<string, number | null>
  if (e.kind === 'extreme_listing' && d.robust_price && d.naive_mean)
    return `A mean would have published ${compact(d.naive_mean)} (×${compact(d.naive_mean / d.robust_price, 2)}); robust price ${compact(d.robust_price)}.`
  if (e.kind === 'rejected_price' && d.rejected_price && d.reference)
    return `${compact(d.rejected_price)} vs ${(e.detail as Record<string, string>).reason === 'cross_server' ? 'other servers' : 'its own history'} at ${compact(d.reference)} — not published.`
  if (e.kind === 'thin_market_spike' && d.price && d.trailing_median)
    return `Price ${compact(d.price)} vs trailing median ${compact(d.trailing_median)}.`
  return ''
}
