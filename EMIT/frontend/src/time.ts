import { useEffect, useState } from 'react'

/** "1:05", "12:00", "1:02:03". Never negative. */
export function clock(totalSeconds: number): string {
  const s = Math.max(0, Math.ceil(totalSeconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = String(s % 60).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`
}

/** An honest, deliberately coarse wait: we know the admission rate, not what the people ahead will do. */
export function roughWait(seconds: number): string {
  if (seconds < 10) return 'a few seconds'
  if (seconds < 50) return `about ${Math.round(seconds / 10) * 10} seconds`
  if (seconds < 90) return 'about a minute'
  if (seconds < 3600) return `about ${Math.round(seconds / 60)} minutes`
  return 'more than an hour'
}

export function money(cents: number): string {
  return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(cents / 100)
}

/** Difference between the server's clock and ours, so countdowns do not trust the device clock. */
export function clockOffsetMs(serverTime: string): number {
  return Date.parse(serverTime) - Date.now()
}

/** Re-renders the caller every `intervalMs` and returns the current server-adjusted time in ms. */
export function useServerNow(offsetMs: number, intervalMs = 250): number {
  const [now, setNow] = useState(() => Date.now() + offsetMs)
  useEffect(() => {
    const tick = () => setNow(Date.now() + offsetMs)
    const id = setInterval(tick, intervalMs)
    return () => clearInterval(id)
  }, [offsetMs, intervalMs])
  return now
}
