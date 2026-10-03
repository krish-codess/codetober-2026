import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const world = {
  world_id: 'eve', name: 'EVE', price_source: 'trade_history', is_synthetic: false, currency: 'ISK',
  servers: [{ server_id: 'eve-domain', name: 'Domain' }], divisions: [{ division_id: 'fuel', label: 'Fuel' }],
  activities: [{ activity_id: 'eve-ratting', label: 'Ratting' }], items: [{ item_id: 34, name: 'Tritanium', division_id: 'minerals' }],
  first_day: '2026-01-01', freshness: { last_day: '2026-01-10', age_hours: 2000, stale: true },
}
const indexBody = (points: unknown[]) => ({
  series: { world_id: 'eve', server_id: 'all', division_id: 'all', series_id: 1 }, method_version: 'gs-1.0', as_of: null,
  freshness: world.freshness, points,
})
const pts = Array.from({ length: 10 }, (_, i) => ({
  day: `2026-01-${String(i + 1).padStart(2, '0')}`, value: 100 + i, coverage: i === 3 ? 0.7 : 1,
  status: i === 3 ? 'partial' : 'ok', vintage: 1, revised: false,
}))

function api(overrides: Record<string, () => Response | Promise<Response>> = {}) {
  return vi.fn(async (url: string) => {
    for (const [k, f] of Object.entries(overrides)) if (url.includes(k)) return f()
    const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200 })
    if (url.includes('/v1/worlds')) return json([world])
    if (url.includes('/v1/index')) return json(indexBody(pts))
    if (url.includes('/v1/shocks')) return json({ series: indexBody([]).series, shocks: [] })
    if (url.includes('/v1/inflation')) return json({ series: indexBody([]).series, window_days: 30, points: [{ day: '2026-01-10', rate: 0.01, annualized: 0.12 }] })
    if (url.includes('/v1/patches')) return json({ items: [], page: { next_cursor: null, limit: 200 } })
    return new Response('{}', { status: 404 })
  })
}

beforeEach(() => window.history.replaceState(null, '', '/'))
afterEach(() => vi.unstubAllGlobals())

describe('App states', () => {
  it('shows determinate progress, then data with stale and partial-coverage banners', async () => {
    vi.stubGlobal('fetch', api())
    render(<App />)
    expect(screen.getByText(/Loading economies: 0 of 1/)).toBeInTheDocument()
    expect(await screen.findByText(/Loading index, shocks, inflation and patch notes: \d of 5/)).toBeInTheDocument()
    expect(await screen.findByRole('application', { name: /Price index/ })).toBeInTheDocument()
    expect(screen.getByText(/Data is stale/)).toBeInTheDocument()
    expect(screen.getByText(/1 day\(s\) with partial coverage/)).toBeInTheDocument()
    expect(screen.getByText('+12.0%', { exact: false })).toBeInTheDocument()
  })

  it('shows an actionable error with request id and recovers on retry', async () => {
    let fail = true
    const fetchMock = api({
      '/v1/index': () =>
        fail
          ? new Response(JSON.stringify({ error: { code: 'database_unavailable', message: 'db down', request_id: 'rid9' } }), { status: 503 })
          : new Response(JSON.stringify(indexBody(pts)), { status: 200 }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<App />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('temporarily unavailable')
    expect(alert).toHaveTextContent('rid9')
    fail = false
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('application', { name: /Price index/ })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('explains an empty range instead of rendering a blank chart', async () => {
    vi.stubGlobal('fetch', api({ '/v1/index': () => new Response(JSON.stringify(indexBody([])), { status: 200 }) }))
    render(<App />)
    expect(await screen.findByText(/No index values are published/)).toBeInTheDocument()
    expect(screen.queryByRole('application')).not.toBeInTheDocument()
  })

  it('tabs follow the WAI-ARIA keyboard pattern and the view is in the URL', async () => {
    vi.stubGlobal('fetch', api())
    const user = userEvent.setup()
    render(<App />)
    const first = await screen.findByRole('tab', { name: 'Price index' })
    first.focus()
    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'Purchasing power' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Purchasing power' })).toHaveFocus()
    expect(window.location.search).toContain('tab=power')
    await user.keyboard('{End}')
    expect(screen.getByRole('tab', { name: 'Market integrity' })).toHaveAttribute('aria-selected', 'true')
  })

  it('does not refetch on re-render: one request per resource', async () => {
    const fetchMock = api()
    vi.stubGlobal('fetch', fetchMock)
    const { rerender } = render(<App />)
    await screen.findByRole('application', { name: /Price index/ })
    const n = fetchMock.mock.calls.length
    rerender(<App />)
    await waitFor(() => expect(fetchMock.mock.calls.length).toBe(n))
    const worldsCalls = fetchMock.mock.calls.filter(([u]) => String(u).includes('/v1/worlds')).length
    expect(worldsCalls).toBe(1)
  })
})
