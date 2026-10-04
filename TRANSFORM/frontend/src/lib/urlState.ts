import { useCallback, useEffect, useState } from 'react'

/** A piece of state mirrored into the query string, so every view is linkable and survives reload. */
export function useUrlState(key: string, initial: string): [string, (v: string) => void] {
  const read = () => new URLSearchParams(window.location.search).get(key) ?? initial
  const [value, setValue] = useState(read)
  useEffect(() => {
    const onPop = () => setValue(read())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const set = useCallback(
    (v: string) => {
      const params = new URLSearchParams(window.location.search)
      if (v === initial) params.delete(key)
      else params.set(key, v)
      const qs = params.toString()
      window.history.replaceState(null, '', qs ? `?${qs}` : window.location.pathname)
      setValue(v)
    },
    [key, initial],
  )
  return [value, set]
}
