import { useCallback, useEffect, useState } from 'react'
import { ApiError, getJSON, invalidate } from './api'

export interface ApiState<T> {
  data: T | undefined
  error: ApiError | undefined
  loading: boolean
  reload: () => void
}

/** Fetch `path` (null = skip). Keeps the previous data while a new path loads so the UI never flashes empty. */
export function useApi<T>(path: string | null): ApiState<T> {
  const [state, setState] = useState<{ data?: T; error?: ApiError; loading: boolean }>({ loading: path !== null })
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    if (path === null) {
      setState({ loading: false })
      return
    }
    let live = true
    setState((s) => ({ data: s.data, loading: true }))
    getJSON<T>(path, { force: nonce > 0 })
      .then((data) => live && setState({ data, loading: false }))
      .catch((error: unknown) => {
        if (!live) return
        const e = error instanceof ApiError ? error : new ApiError(0, 'client_error', String(error), null)
        setState({ error: e, loading: false })
      })
    return () => {
      live = false
    }
  }, [path, nonce])

  const reload = useCallback(() => {
    if (path) invalidate(path)
    setNonce((n) => n + 1)
  }, [path])

  return { data: state.data, error: state.error, loading: state.loading, reload }
}
