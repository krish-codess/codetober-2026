import { useRef } from 'react'
import { ErrorPanel, Progress } from './components/States'
import { invalidate } from './lib/api'
import type { World } from './lib/api'
import { day } from './lib/format'
import { useUrlState } from './lib/urlState'
import { useApi } from './lib/useApi'
import { IndexPage } from './pages/IndexPage'
import { InflationPage } from './pages/InflationPage'
import { IntegrityPage } from './pages/IntegrityPage'
import { PowerPage } from './pages/PowerPage'

const TABS = [
  { id: 'index', label: 'Price index' },
  { id: 'power', label: 'Purchasing power' },
  { id: 'inflation', label: 'Inflation' },
  { id: 'integrity', label: 'Market integrity' },
] as const

export default function App() {
  const [tab, setTab] = useUrlState('tab', 'index')
  const [worldId, setWorldId] = useUrlState('world', 'eve')
  const [server, setServer] = useUrlState('server', 'all')
  const worlds = useApi<World[]>('/v1/worlds')
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([])
  const panelRef = useRef<HTMLDivElement>(null)
  const world = worlds.data?.find((w) => w.world_id === worldId) ?? worlds.data?.[0]
  const serverOk = server === 'all' || world?.servers.some((s) => s.server_id === server)

  function onTabKey(e: React.KeyboardEvent, i: number) {
    const n = TABS.length
    const next = e.key === 'ArrowRight' ? (i + 1) % n : e.key === 'ArrowLeft' ? (i - 1 + n) % n : e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : -1
    if (next < 0) return
    e.preventDefault()
    setTab(TABS[next].id)
    tabRefs.current[next]?.focus()
  }

  function switchWorld(id: string) {
    setWorldId(id)
    setServer('all')
  }

  return (
    <div className="app">
      <a href="#main" className="skip">
        Skip to content
      </a>
      <header className="top">
        <div className="brand">
          <span className="logo" aria-hidden="true">
            Au
          </span>
          <div>
            <h1>GOLD STANDARD</h1>
            <p className="tagline">A consumer price index for video game economies</p>
          </div>
        </div>
        {worlds.data && (
          <fieldset className="segmented world">
            <legend className="sr-only">Economy</legend>
            {worlds.data.map((w) => (
              <label key={w.world_id}>
                <input type="radio" name="world" checked={world?.world_id === w.world_id} onChange={() => switchWorld(w.world_id)} />
                {w.is_synthetic ? 'Simulated shard' : 'EVE Online (real)'}
              </label>
            ))}
          </fieldset>
        )}
        <button
          type="button"
          className="ghost"
          onClick={() => {
            invalidate()
            worlds.reload()
          }}
          title="Refetch everything (the cache otherwise keeps data for 60 s)"
        >
          ↻ Refresh
        </button>
      </header>

      <nav aria-label="Views">
        <div role="tablist" aria-label="Views" className="tabs">
          {TABS.map((t, i) => (
            <button
              key={t.id}
              ref={(el) => {
                tabRefs.current[i] = el
              }}
              role="tab"
              id={`tab-${t.id}`}
              aria-selected={tab === t.id}
              aria-controls="main"
              tabIndex={tab === t.id ? 0 : -1}
              onClick={() => {
                setTab(t.id)
                panelRef.current?.focus()
              }}
              onKeyDown={(e) => onTabKey(e, i)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </nav>

      <main id="main" role="tabpanel" aria-labelledby={`tab-${tab}`} tabIndex={-1} ref={panelRef}>
        {worlds.loading && !worlds.data && <Progress done={0} total={1} what="economies" />}
        {worlds.error && <ErrorPanel error={worlds.error} onRetry={worlds.reload} />}
        {world && (
          <>
            <p className="world-note muted">
              {world.name}: {world.is_synthetic ? 'simulated auction snapshots with known ground truth' : 'real ESI daily trade history'} ·
              {' '}
              {world.servers.length} servers · data through {world.freshness.last_day ? day(world.freshness.last_day) : '—'}
            </p>
            {tab === 'index' && <IndexPage key={world.world_id} world={world} server={serverOk ? server : 'all'} setServer={setServer} />}
            {tab === 'power' && <PowerPage key={world.world_id} world={world} server={serverOk ? server : 'all'} setServer={setServer} />}
            {tab === 'inflation' && <InflationPage key={world.world_id} world={world} server={serverOk ? server : 'all'} setServer={setServer} />}
            {tab === 'integrity' && <IntegrityPage key={world.world_id} world={world} server={serverOk ? server : 'all'} setServer={setServer} />}
          </>
        )}
      </main>
      <footer className="muted">
        Index: chain-linked Laspeyres, quarterly volume weights, robust prices. Published values are append-only — a
        revision is a new vintage, never an overwrite. Data: EVE Online ESI (CCP Games) and a calibrated simulation.
      </footer>
    </div>
  )
}
