import { useMemo, useState } from 'react'
import { LineChart } from '../components/LineChart'
import { Empty, ErrorPanel, Progress, StaleBanner } from '../components/States'
import { qs } from '../lib/api'
import type { PurchasingPower, World } from '../lib/api'
import { arrow, compact, day, hours, pct } from '../lib/format'
import { useApi } from '../lib/useApi'

const DEFAULT_ITEMS = [34, 587, 44992]
const MAX_ITEMS = 5

export function PowerPage({ world, server, setServer }: { world: World; server: string; setServer: (s: string) => void }) {
  const activeServer = server === 'all' ? world.servers[0]?.server_id ?? '' : server
  const [activity, setActivity] = useState(world.activities.at(-1)?.activity_id ?? '')
  const [items, setItems] = useState<number[]>(DEFAULT_ITEMS.filter((i) => world.items.some((x) => x.item_id === i)))
  const [log, setLog] = useState(true)
  const act = world.activities.some((a) => a.activity_id === activity) ? activity : world.activities[0]?.activity_id

  const path =
    act && activeServer
      ? `/v1/purchasing-power${qs({ world: world.world_id, server: activeServer, activity: act, items: items.join(',') })}`
      : null
  const pp = useApi<PurchasingPower>(path)
  const pts = useMemo(() => (pp.data?.points ?? []).filter((p) => p.wage != null), [pp.data])
  const first = pts[0]
  const last = pts.at(-1)
  const name = (id: number) => world.items.find((i) => i.item_id === id)?.name ?? `#${id}`
  const actLabel = world.activities.find((a) => a.activity_id === act)?.label ?? ''
  const powerChange = first?.hours_per_basket && last?.hours_per_basket ? first.hours_per_basket / last.hours_per_basket - 1 : null

  function toggle(id: number) {
    setItems((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : cur.length >= MAX_ITEMS ? cur : [...cur, id]))
  }

  return (
    <section aria-labelledby="pp-h">
      <h2 id="pp-h">Purchasing power calculator</h2>
      <p className="lede">
        What does an hour of farming buy, and how has that changed? Wages are an activity's nominal pay plus what its
        output sells for that day; prices are the published robust prices for the server.
      </p>
      <div className="controls">
        <label>
          Server
          <select value={activeServer} onChange={(e) => setServer(e.target.value)}>
            {world.servers.map((s) => (
              <option key={s.server_id} value={s.server_id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Activity
          <select value={act} onChange={(e) => setActivity(e.target.value)}>
            {world.activities.map((a) => (
              <option key={a.activity_id} value={a.activity_id}>
                {a.label}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input type="checkbox" checked={log} onChange={(e) => setLog(e.target.checked)} /> Log scale
        </label>
      </div>
      <fieldset className="items">
        <legend>
          Items to price in labour-hours ({items.length}/{MAX_ITEMS})
        </legend>
        {world.items.map((i) => (
          <label key={i.item_id} className={items.includes(i.item_id) ? 'chip on' : 'chip'}>
            <input
              type="checkbox"
              checked={items.includes(i.item_id)}
              disabled={!items.includes(i.item_id) && items.length >= MAX_ITEMS}
              onChange={() => toggle(i.item_id)}
            />
            {i.name}
          </label>
        ))}
      </fieldset>

      <StaleBanner freshness={world.freshness} />
      {pp.loading && <Progress done={0} total={1} what="wages and prices" />}
      {pp.error && <ErrorPanel error={pp.error} onRetry={pp.reload} />}
      {!pp.loading && !pp.error && pts.length === 0 && (
        <Empty>
          No wage can be computed for this server yet: the activity's output has no published price on any day. Pick
          a deeper server.
        </Empty>
      )}

      {first && last && pp.data && (
        <>
          <div className="answer" aria-live="polite">
            <p>
              On <strong>{day(last.day)}</strong>, one hour of <strong>{actLabel}</strong> earned{' '}
              <strong>{compact(last.wage)} ISK</strong> and bought{' '}
              {last.items
                .filter((i) => i.units_per_hour != null)
                .map((i, k, arr) => (
                  <span key={i.item_id}>
                    <strong>{compact(i.units_per_hour)}</strong> {name(i.item_id)}
                    {k < arr.length - 2 ? ', ' : k === arr.length - 2 ? ' or ' : ''}
                  </span>
                ))}
              .
            </p>
            <p>
              The reference basket costs <strong>{hours(last.hours_per_basket)}</strong> of this work, against{' '}
              {hours(first.hours_per_basket)} on {day(first.day)}: purchasing power {arrow(powerChange)}{' '}
              <strong>{pct(powerChange)}</strong>.
            </p>
          </div>
          <LineChart
            label={`Hours of ${actLabel} needed to buy the reference basket`}
            unit=" h"
            series={[{ key: 'h', label: 'Hours of work per basket', values: pts.map((p) => ({ day: p.day, value: p.hours_per_basket ?? null })) }]}
          />
          <LineChart
            label={`Units of each item one hour of ${actLabel} buys`}
            log={log}
            series={items.map((id, k) => ({
              key: String(id),
              label: `${name(id)} per hour`,
              dash: ['', '6 3', '2 3', '10 3 2 3', '1 2'][k],
              values: pts.map((p) => ({ day: p.day, value: p.items.find((i) => i.item_id === id)?.units_per_hour ?? null })),
            }))}
          />
          <div className="table-wrap">
            <table>
              <caption>Labour-hours per item, first vs last day</caption>
              <thead>
                <tr>
                  <th scope="col">Item</th>
                  <th scope="col">Price {day(first.day)}</th>
                  <th scope="col">Price {day(last.day)}</th>
                  <th scope="col">Work per unit then</th>
                  <th scope="col">Work per unit now</th>
                  <th scope="col">Change</th>
                </tr>
              </thead>
              <tbody>
                {items.map((id) => {
                  const a = first.items.find((i) => i.item_id === id)
                  const b = last.items.find((i) => i.item_id === id)
                  const ch = a?.hours_per_unit && b?.hours_per_unit ? b.hours_per_unit / a.hours_per_unit - 1 : null
                  return (
                    <tr key={id}>
                      <th scope="row">{name(id)}</th>
                      <td>{compact(a?.price)}</td>
                      <td>{compact(b?.price)}</td>
                      <td>{hours(a?.hours_per_unit)}</td>
                      <td>{hours(b?.hours_per_unit)}</td>
                      <td>
                        {arrow(ch)} {pct(ch)} {ch != null && <span className="muted">{ch > 0 ? 'more work' : 'less work'}</span>}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {pp.data.rates.length > 1 && (
            <p className="muted">
              Nominal pay changed {pp.data.rates.length - 1} time(s) with patches:{' '}
              {pp.data.rates.map((r) => `${day(r.effective_from)}: ${compact(r.isk_per_hour)} ISK/h`).join(' → ')}.
            </p>
          )}
        </>
      )}
    </section>
  )
}
