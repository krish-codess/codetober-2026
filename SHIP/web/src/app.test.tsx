import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearCache, token, type Run } from './api'
import { App } from './App'
import { downsample, niceMax } from './Chart'
import { Compat } from './Compat'
import { eta, Migrations, stepStatus } from './Migrations'

const run = (over: Partial<Run> = {}): Run => ({
  id: 2,
  name: '02_amount_to_cents',
  state: 'expanded',
  reason: '',
  compatible_app_versions: ['v2'],
  settings: { max_lock_wait_ms: 3000, lock_budget_s: 10, max_rollbacks_per_s: 20 },
  rows_total: 1000,
  rows_done: 1000,
  created_at: '2026-10-05T10:00:00Z',
  started_at: '2026-10-05T10:00:01Z',
  ddl_done_at: '2026-10-05T10:00:02Z',
  expanded_at: '2026-10-05T10:00:30Z',
  verification: {
    ok: true,
    at: '2026-10-05T10:00:31Z',
    duration_ms: 900,
    checks: [{ table: 'orders', column: 'amount', rows: 1000, mismatches: 0, unbackfilled: 0, lossy: 7, examples: [] }],
  },
  ...over,
})

type Route = (init?: RequestInit) => { status?: number; body: unknown } | Promise<never>
let routes: Record<string, Route>
const calls: string[] = []

beforeEach(() => {
  token.set('t')
  clearCache()
  calls.length = 0
  routes = {
    'GET /api/v1/migrations?limit=50': () => ({ body: { items: [run()] } }),
    'GET /api/v1/migrations/2': () => ({ body: run() }),
    'GET /api/v1/migrations/2/samples?limit=2000': () => ({ body: { items: [] } }),
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const key = `${init?.method ?? 'GET'} ${url}`
      calls.push(key)
      const route = routes[key]
      if (!route) return new Response(JSON.stringify({ title: 'Not Found', status: 404, detail: 'no route ' + key }), { status: 404 })
      const { status = 200, body } = await route(init)
      return new Response(JSON.stringify(body), { status })
    }),
  )
  // jsdom has no modal dialogs; opening and closing is all these tests need.
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '')
  }
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.removeAttribute('open')
  }
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  sessionStorage.clear()
})

describe('pure logic', () => {
  it('places a run on the right step', () => {
    expect(stepStatus(run({ state: 'expanding', ddl_done_at: undefined, expanded_at: undefined }))).toEqual(['current', 'pending', 'pending', 'pending'])
    expect(stepStatus(run({ state: 'expanding', expanded_at: undefined }))).toEqual(['done', 'current', 'pending', 'pending'])
    expect(stepStatus(run())).toEqual(['done', 'done', 'current', 'pending'])
    expect(stepStatus(run({ state: 'completed', contract_started_at: 'x', finished_at: 'y' }))).toEqual(['done', 'done', 'done', 'done'])
    expect(stepStatus(run({ state: 'reverted', expanded_at: undefined, finished_at: 'y' }))).toEqual(['done', 'failed', 'pending', 'pending'])
  })

  it('estimates time left from rows done so far, and only while backfilling', () => {
    const at = Date.parse('2026-10-05T10:00:12Z') // 10 s after the backfill began
    expect(eta(run({ state: 'expanding', rows_done: 250 }), at)).toBe('about 30 s left')
    expect(eta(run({ state: 'expanding', rows_done: 10 }), at)).toBe('about 17 min left')
    expect(eta(run({ state: 'expanding', rows_done: 0 }), at)).toBe('')
    expect(eta(run(), at)).toBe('')
  })

  it('keeps spikes when downsampling and ends axes on round numbers', () => {
    const pts = Array.from({ length: 1000 }, (_, i) => ({ x: i, y: i === 637 ? 99 : 1 }))
    const small = downsample(pts, 100)
    expect(small.length).toBeLessThanOrEqual(100)
    expect(Math.max(...small.map((p) => p.y))).toBe(99)
    expect([0, 0.3, 7, 20, 23, 4100].map(niceMax)).toEqual([1, 0.5, 10, 20, 50, 5000])
  })
})

describe('Migrations', () => {
  it('says so when there are no runs', async () => {
    routes['GET /api/v1/migrations?limit=50'] = () => ({ body: { items: [] } })
    render(<Migrations />)
    expect(await screen.findByText(/No migrations yet/)).toBeTruthy()
    expect(screen.getByText(/Select a run/)).toBeTruthy()
  })

  it('shows progress, verification and the reason a run stopped', async () => {
    routes['GET /api/v1/migrations/2'] = () => ({
      body: run({ state: 'reverted', reason: 'guard: an application session was blocked for 4000 ms', rows_done: 400, expanded_at: undefined }),
    })
    render(<Migrations />)
    expect((await screen.findByRole('alert')).textContent).toContain('blocked for 4000 ms')
    expect(screen.getByLabelText(/Backfill: 400 of about 1,000 rows \(40%\)/)).toBeTruthy()
    expect(within(screen.getByRole('table')).getByText('7')).toBeTruthy() // quarantined rows are reported, not hidden
    expect((screen.getByRole('button', { name: /Complete/ }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: /Abort/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('asks before completing, then shows why the controller refused', async () => {
    const user = userEvent.setup()
    routes['POST /api/v1/migrations/2/complete'] = () => ({
      status: 409,
      body: {
        title: 'Conflict',
        status: 409,
        detail: 'connected application versions cannot run on schema version 02_amount_to_cents; stop them first',
        errors: [{ message: 'shop v1 on 01_initial (8 sessions)' }],
      },
    })
    render(<Migrations />)
    await user.click(await screen.findByRole('button', { name: /Complete \(drop old column\)/ }))
    expect(calls).not.toContain('POST /api/v1/migrations/2/complete') // nothing happens until confirmed
    await user.click(within(screen.getByRole('dialog', { hidden: true })).getByRole('button', { name: 'Complete', hidden: true }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Refused.')
    expect(alert.textContent).toContain('shop v1 on 01_initial (8 sessions)')
    // the run is refetched straight after the attempt, not at the next poll
    expect(calls.filter((c) => c === 'GET /api/v1/migrations/2').length).toBeGreaterThan(1)
  })

  it('keeps the last data and flags it stale when refreshing fails', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<Migrations />)
    expect(await screen.findByText('Old and new agree')).toBeTruthy()
    const down: Route = () => Promise.reject(new TypeError('network'))
    routes['GET /api/v1/migrations?limit=50'] = down
    routes['GET /api/v1/migrations/2'] = down
    await act(() => vi.advanceTimersByTimeAsync(20000))
    expect(screen.getAllByText(/Out of date/).length).toBeGreaterThan(0)
    expect(screen.getByText('Old and new agree')).toBeTruthy() // stale data stays on screen
  })

  it('shows an error with a retry when the first load fails, and recovers', async () => {
    const user = userEvent.setup()
    routes['GET /api/v1/migrations?limit=50'] = () => ({
      status: 500,
      body: { title: 'Internal Server Error', status: 500, detail: 'internal error; see the controller log' },
    })
    render(<Migrations />)
    expect((await screen.findByRole('alert')).textContent).toContain('see the controller log')
    routes['GET /api/v1/migrations?limit=50'] = () => ({ body: { items: [run()] } })
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('button', { name: /02_amount_to_cents/ })).toBeTruthy()
  })

  it('plans from a desired schema, then starts the planned migration', async () => {
    const user = userEvent.setup()
    routes['POST /api/v1/plans'] = () => ({
      body: {
        name: '03_x',
        compatible_app_versions: ['v2'],
        operations: [{ create_index: {} }],
        steps: [{ phase: 'expand', table: 'orders', action: 'create index concurrently idx', lock: 'SHARE UPDATE EXCLUSIVE', blocks: 'nothing' }],
        estimates: [{ table: 'orders', rows: 1000, bytes: 1, backfill_seconds: 0 }],
      },
    })
    routes['POST /api/v1/migrations'] = (init) => {
      expect(JSON.parse(String(init?.body))).toEqual({ name: '03_x', compatible_app_versions: ['v2'], operations: [{ create_index: {} }] })
      return { status: 202, body: run({ id: 3, name: '03_x', state: 'pending' }) }
    }
    render(<Migrations />)
    const box = await screen.findByLabelText(/Desired schema/)
    await user.click(box)
    await user.paste('{not json')
    await user.click(screen.getByRole('button', { name: 'Show the plan' }))
    expect((await screen.findByText(/not valid JSON/)).closest('[role=alert]')).toBeTruthy()
    await user.clear(box)
    await user.paste('{"name":"03_x"}')
    await user.click(screen.getByRole('button', { name: 'Show the plan' }))
    expect(await screen.findByText('create index concurrently idx')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Start this migration' }))
    await waitFor(() => expect(calls).toContain('POST /api/v1/migrations'))
  })
})

describe('Compat', () => {
  it('states compatibility in words and flags connections that should not exist', async () => {
    routes['GET /api/v1/compat'] = () => ({
      body: {
        schema_versions: [
          { version: '01_initial', live: false },
          { version: '02_amount_to_cents', live: true },
        ],
        app_versions: ['v1', 'v2'],
        cells: [
          { app_version: 'v1', schema_version: '01_initial', compatible: true, sessions: 0 },
          { app_version: 'v1', schema_version: '02_amount_to_cents', compatible: false, sessions: 2 },
          { app_version: 'v2', schema_version: '01_initial', compatible: false, sessions: 0 },
          { app_version: 'v2', schema_version: '02_amount_to_cents', compatible: true, sessions: 8 },
        ],
      },
    })
    render(<Compat />)
    const row = (await screen.findByRole('rowheader', { name: 'v1' })).closest('tr')!
    expect(within(row).getByText(/2 connections \(should not be here\)/)).toBeTruthy()
    expect(screen.getAllByText(/not compatible/).length).toBe(2)
    expect(screen.getByText(/retired/)).toBeTruthy()
    expect(within(screen.getByRole('rowheader', { name: 'v2' }).closest('tr')!).getByText(/8 connections/)).toBeTruthy()
  })
})

describe('App', () => {
  it('asks for a token and rejects a wrong one without entering', async () => {
    const user = userEvent.setup()
    token.clear()
    routes['GET /api/v1/compat'] = () => ({ status: 401, body: { title: 'Unauthorized', status: 401, detail: 'missing or invalid bearer token' } })
    render(<App />)
    await user.type(screen.getByLabelText('API token'), 'wrong')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect((await screen.findByRole('alert')).textContent).toContain('not accepted')
    expect(token.get()).toBe('')
    expect(screen.queryByRole('navigation')).toBeNull()
  })
})
