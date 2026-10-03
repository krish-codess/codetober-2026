import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, TTL_MS, getAllPages, getJSON, invalidate } from './api'

function respond(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' }, ...init })
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('getJSON cache', () => {
  it('serves repeated reads inside the TTL from cache (no fetch on render)', async () => {
    const fetchMock = vi.fn(async () => respond({ n: 1 }, { headers: { ETag: '"a"' } }))
    vi.stubGlobal('fetch', fetchMock)
    await getJSON('/v1/x')
    await getJSON('/v1/x')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('dedupes concurrent requests for the same URL', async () => {
    const fetchMock = vi.fn(async () => respond({ n: 1 }))
    vi.stubGlobal('fetch', fetchMock)
    const [a, b] = await Promise.all([getJSON('/v1/y'), getJSON('/v1/y')])
    expect(a).toEqual(b)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('revalidates with If-None-Match after the TTL and reuses data on 304', async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(respond({ n: 1 }, { headers: { ETag: '"v1"' } }))
      .mockResolvedValueOnce(new Response(null, { status: 304 }))
    vi.stubGlobal('fetch', fetchMock)
    await getJSON('/v1/z')
    vi.advanceTimersByTime(TTL_MS + 1)
    expect(await getJSON('/v1/z')).toEqual({ n: 1 })
    expect(fetchMock.mock.calls[1][1].headers['If-None-Match']).toBe('"v1"')
  })

  it('invalidate() forces a refetch', async () => {
    const fetchMock = vi.fn(async () => respond({ n: 1 }))
    vi.stubGlobal('fetch', fetchMock)
    await getJSON('/v1/w')
    invalidate('/v1/w')
    await getJSON('/v1/w')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('maps the API error envelope to ApiError with code and request id', async () => {
    vi.stubGlobal('fetch', async () =>
      respond({ error: { code: 'unknown_world', message: 'nope', request_id: 'r1' } }, { status: 404 }),
    )
    await expect(getJSON('/v1/e')).rejects.toMatchObject({ status: 404, code: 'unknown_world', requestId: 'r1' })
  })

  it('reports network failure as a retryable ApiError, not a crash', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('Failed to fetch')
    })
    const err = await getJSON('/v1/n').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).code).toBe('network_error')
  })

  it('follows keyset cursors until exhausted', async () => {
    const pages: Record<string, unknown> = {
      '/api/v1/p?cursor=': { items: [1, 2], page: { next_cursor: 'c2' } },
      '/api/v1/p?cursor=c2': { items: [3], page: { next_cursor: null } },
    }
    vi.stubGlobal('fetch', async (url: string) => respond(pages[url]))
    const seen: number[] = []
    const all = await getAllPages<number>((c) => `/v1/p?cursor=${c ?? ''}`, (n) => seen.push(n))
    expect(all).toEqual([1, 2, 3])
    expect(seen).toEqual([1, 2])
  })
})
