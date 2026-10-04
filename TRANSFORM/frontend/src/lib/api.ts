/**
 * Typed API access with a deliberate cache.
 *
 * - Types come from src/api-types.ts, generated from the API's OpenAPI spec (npm run gen:api).
 * - Each URL is cached for TTL_MS. Within the TTL, re-renders and re-mounts reuse the cached value
 *   (no fetch on render). After it, the next read revalidates with If-None-Match: an unchanged
 *   resource costs a 304 with no body. Concurrent reads of one URL share one request.
 * - invalidate() is the explicit escape hatch (the Refresh button); nothing else evicts.
 */
import type { components } from '../api-types'

type S = components['schemas']
export type World = S['WorldOut']
export type IndexResponse = S['IndexResponse']
export type IndexPoint = S['IndexPoint']
export type PatchPage = S['PatchPage']
export type Patch = S['PatchOut']
export type ShocksResponse = S['ShocksResponse']
export type Shock = S['ShockOut']
export type InflationResponse = S['InflationResponse']
export type InflationMatrix = S['InflationMatrix']
export type PurchasingPower = S['PurchasingPowerResponse']
export type ManipulationPage = S['ManipulationPage']
export type Flows = S['FlowsResponse']
export type Freshness = S['Freshness']

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? '/api'
export const TTL_MS = 60_000

export class ApiError extends Error {
  status: number
  code: string
  requestId: string | null
  constructor(status: number, code: string, message: string, requestId: string | null) {
    super(message)
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

interface Entry {
  data: unknown
  etag: string | null
  at: number
}

const cache = new Map<string, Entry>()
const inflight = new Map<string, Promise<unknown>>()

export function clearCache(): void {
  cache.clear()
  inflight.clear()
}

export function invalidate(prefix = ''): void {
  for (const key of [...cache.keys()]) if (key.startsWith(API_BASE + prefix)) cache.delete(key)
}

export function cachedAt(path: string): number | null {
  return cache.get(API_BASE + path)?.at ?? null
}

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const u = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== '') u.set(k, String(v))
  const s = u.toString()
  return s ? `?${s}` : ''
}

export async function getJSON<T>(path: string, opts: { force?: boolean } = {}): Promise<T> {
  const url = API_BASE + path
  const hit = cache.get(url)
  if (!opts.force && hit && Date.now() - hit.at < TTL_MS) return hit.data as T
  const pending = inflight.get(url)
  if (pending) return pending as Promise<T>
  const request = (async () => {
    const headers: Record<string, string> = { Accept: 'application/json' }
    if (hit?.etag) headers['If-None-Match'] = hit.etag
    let res: Response
    try {
      res = await fetch(url, { headers })
    } catch {
      throw new ApiError(0, 'network_error', 'The server could not be reached. Check your connection and retry.', null)
    }
    if (res.status === 304 && hit) {
      hit.at = Date.now()
      return hit.data
    }
    if (!res.ok) {
      let code = 'http_error'
      let message = `Request failed (${res.status})`
      let requestId = res.headers.get('X-Request-ID')
      try {
        const body = (await res.json()) as { error?: { code: string; message: string; request_id: string } }
        if (body.error) ({ code, message } = body.error)
        requestId = body.error?.request_id ?? requestId
      } catch {
        /* non-JSON error page (proxy): keep the generic message */
      }
      throw new ApiError(res.status, code, message, requestId)
    }
    const data: unknown = await res.json()
    cache.set(url, { data, etag: res.headers.get('ETag'), at: Date.now() })
    return data
  })().finally(() => inflight.delete(url))
  inflight.set(url, request)
  return request as Promise<T>
}

/** Follow keyset cursors until exhausted (bounded: at most maxPages requests). */
export async function getAllPages<T>(
  path: (cursor: string | null) => string,
  onPage?: (n: number) => void,
  maxPages = 20,
): Promise<T[]> {
  const out: T[] = []
  let cursor: string | null = null
  for (let n = 1; n <= maxPages; n++) {
    const page: { items: T[]; page: { next_cursor?: string | null } } = await getJSON(path(cursor))
    out.push(...page.items)
    onPage?.(n)
    cursor = page.page.next_cursor ?? null
    if (!cursor) break
  }
  return out
}
