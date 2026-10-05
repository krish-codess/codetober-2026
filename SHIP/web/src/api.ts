import { useCallback, useEffect, useRef, useState } from 'react'

export type RunState = 'pending' | 'expanding' | 'expanded' | 'contracting' | 'completed' | 'reverting' | 'reverted' | 'failed'

export interface ColumnCheck {
  table: string
  column: string
  rows: number
  mismatches: number
  unbackfilled: number
  lossy: number
  examples: string[]
}
export interface Verification {
  ok: boolean
  at: string
  duration_ms: number
  checks: ColumnCheck[]
}
export interface Run {
  id: number
  name: string
  state: RunState
  reason: string
  compatible_app_versions: string[]
  settings: Record<string, number>
  rows_total: number
  rows_done: number
  verification?: Verification
  created_at: string
  started_at?: string
  ddl_done_at?: string
  expanded_at?: string
  contract_started_at?: string
  finished_at?: string
}
export interface Sample {
  at: string
  phase: string
  blocked: number
  max_wait_ms: number
  migrator_wait_ms: number
  active: number
  rollbacks_per_s: number
  rows_done: number
}
export interface Page<T> {
  items: T[]
  next_cursor?: string
}
export interface Matrix {
  schema_versions: { version: string; live: boolean }[]
  app_versions: string[]
  cells: { app_version: string; schema_version: string; compatible: boolean; sessions: number }[]
}
export interface PlanStep {
  phase: string
  table: string
  action: string
  lock: string
  blocks: string
}
export interface Plan {
  name: string
  compatible_app_versions: string[]
  operations: unknown[]
  steps: PlanStep[]
  estimates: { table: string; rows: number; bytes: number; backfill_seconds: number }[]
}
export interface DurationPoint {
  run_id: number
  name: string
  state: string
  rows: number
  bytes: number
  ddl_seconds: number
  backfill_seconds: number
  contract_seconds: number
  rows_per_second: number
  dual_write_seconds: number
}
export interface LockWait {
  run_id: number
  name: string
  state: string
  samples: number
  max_blocked: number
  max_wait_ms: number
  p95_wait_ms: number
  blocked_seconds: number
  migrator_wait_seconds: number
}

/** An error response from the controller: an RFC 9457 problem document. */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public details: string[] = [],
  ) {
    super(message)
  }
}

const TOKEN_KEY = 'shipd-token'
// The token lives for the tab only: closing it signs out.
export const token = {
  get: () => sessionStorage.getItem(TOKEN_KEY) ?? '',
  set: (t: string) => sessionStorage.setItem(TOKEN_KEY, t),
  clear: () => sessionStorage.removeItem(TOKEN_KEY),
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  let res: Response
  try {
    res = await fetch('/api' + path, {
      method: init.method ?? 'GET',
      headers: { Authorization: 'Bearer ' + token.get(), ...(init.body !== undefined && { 'Content-Type': 'application/json' }) },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    })
  } catch {
    throw new ApiError(0, 'The controller cannot be reached. Check that it is running; this page retries by itself.')
  }
  const text = await res.text()
  let body: Record<string, unknown> = {}
  try {
    body = text ? JSON.parse(text) : {}
  } catch {
    // a proxy error page, not the controller
  }
  if (!res.ok) {
    const details = Array.isArray(body.errors) ? body.errors.map((e: { message?: string; location?: string }) => [e.location, e.message].filter(Boolean).join(': ')) : []
    throw new ApiError(res.status, String(body.detail ?? body.title ?? `The controller answered ${res.status}.`), details)
  }
  return body as T
}

// One cache for every poller: revisiting a view shows the last answer at once, then refreshes it.
const cache = new Map<string, { data: unknown; at: number }>()
export const clearCache = () => cache.clear()

export interface Polled<T> {
  data?: T
  error?: ApiError
  /** No answer yet, and none cached. */
  loading: boolean
  /** Showing an answer older than three polling intervals: the last refresh failed. */
  stale: boolean
  updatedAt?: number
  /** Refetch now. Call after changing something on the server. */
  refresh: () => void
}

/** Fetch `path` every `ms` milliseconds while the tab is visible. A failed refresh keeps the last data and flags it stale. */
export function usePoll<T>(path: string | null, ms: number): Polled<T> {
  const [, bump] = useState(0)
  const error = useRef<ApiError>(undefined)
  const [tick, setTick] = useState(0)
  const refresh = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    if (!path) return
    let live = true
    const load = async () => {
      if (document.hidden) return
      try {
        cache.set(path, { data: await api<T>(path), at: Date.now() })
        error.current = undefined
      } catch (e) {
        error.current = e as ApiError
      }
      if (live) bump((n) => n + 1)
    }
    error.current = undefined
    void load()
    const timer = setInterval(load, ms)
    document.addEventListener('visibilitychange', load)
    return () => {
      live = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', load)
    }
  }, [path, ms, tick])

  const hit = path ? cache.get(path) : undefined
  return {
    data: hit?.data as T | undefined,
    error: error.current,
    loading: !hit && !error.current,
    stale: !!hit && !!error.current && Date.now() - hit.at > 3 * ms,
    updatedAt: hit?.at,
    refresh,
  }
}

/** Guard samples for a run, fetched incrementally: each poll asks only for what came after the last one it has. */
export function useSamples(runId: number, active: boolean): { samples: Sample[]; error?: ApiError } {
  const [state, setState] = useState<{ id: number; samples: Sample[]; error?: ApiError }>({ id: runId, samples: [] })
  const ref = useRef(state)
  ref.current = state.id === runId ? state : { id: runId, samples: [] }

  useEffect(() => {
    let live = true
    const load = async () => {
      if (document.hidden) return
      try {
        for (;;) {
          const have = ref.current.samples
          const cursor = have.length ? '&cursor=' + encodeURIComponent(have[have.length - 1]!.at) : ''
          const page = await api<Page<Sample>>(`/v1/migrations/${runId}/samples?limit=2000${cursor}`)
          if (!live) return
          ref.current = { id: runId, samples: [...have, ...page.items] }
          if (!page.next_cursor) break
        }
      } catch (e) {
        ref.current = { ...ref.current, error: e as ApiError }
      }
      if (live) setState(ref.current)
    }
    void load()
    if (!active) return () => void (live = false)
    const timer = setInterval(load, 1000)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [runId, active])

  return ref.current
}
