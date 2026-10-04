import { useCallback, useEffect, useState } from 'react'
import { ApiError, getJSON, invalidate } from './api'

export interface ApiState<T> {
  data: T | undefined
  error: ApiError | undefined
  loading: boolean
  reload: () => void
}

export function toApiError(e: unknown): ApiError {
  return e instanceof ApiError ? e : new ApiError(0, 'client_error', String(e), null)
}

/**
 * Fetch `path` (null = skip). `loading` is derived (the settled request key differs from the wanted
 * one) instead of being set inside the effect, and the previous data stays visible while a new
 * path loads so the UI never flashes empty between selections.
 */
export function useApi<T>(path: string | null): ApiState<T> {
  const [nonce, setNonce] = useState(0)
  const [settled, setSettled] = useState<{ key: string; data?: T; error?: ApiError } | null>(null)
  const key = path === null ? null : `${path}#${nonce}`

  useEffect(() => {
    if (path === null) return
    let live = true
    const k = `${path}#${nonce}`
    getJSON<T>(path, { force: nonce > 0 })
      .then((data) => live && setSettled({ key: k, data }))
      .catch((e: unknown) => live && setSettled((s) => ({ key: k, data: s?.data, error: toApiError(e) })))
    return () => {
      live = false
    }
  }, [path, nonce])

  const reload = useCallback(() => {
    if (path) invalidate(path)
    setNonce((n) => n + 1)
  }, [path])

  const current = settled?.key === key
  return {
    data: path === null ? undefined : settled?.data,
    error: current ? settled?.error : undefined,
    loading: key !== null && !current,
    reload,
  }
}
