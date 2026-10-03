const UNITS: [number, string][] = [
  [1e12, 'T'],
  [1e9, 'B'],
  [1e6, 'M'],
  [1e3, 'k'],
]

/** 1234567 -> "1.23M" ; small values keep significant digits. */
export function compact(x: number | null | undefined, digits = 3): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '—'
  const a = Math.abs(x)
  for (const [v, u] of UNITS) {
    if (a >= v) {
      const n = x / v
      return `${Math.abs(n) >= 100 ? n.toFixed(0) : n.toPrecision(digits)}${u}`
    }
  }
  return a >= 100 ? x.toFixed(0) : x.toPrecision(digits)
}

export function pct(x: number | null | undefined, digits = 1): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '—'
  const v = x * 100
  return `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(digits)}%`
}

/** Direction as a glyph so meaning never depends on colour alone. */
export function arrow(x: number | null | undefined): string {
  if (x === null || x === undefined || x === 0) return '•'
  return x > 0 ? '▲' : '▼'
}

export function hours(h: number | null | undefined): string {
  if (h === null || h === undefined || !Number.isFinite(h)) return '—'
  if (h * 3600 < 1) return '< 1 s'
  if (h * 60 < 1) return `${Math.round(h * 3600)} s`
  if (h < 1) return `${Math.round(h * 60)} min`
  return `${h.toFixed(h < 10 ? 1 : 0)} h`
}

export function day(d: string): string {
  return new Date(d + 'T00:00:00Z').toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' })
}

export function addDays(iso: string, n: number): string {
  const d = new Date(iso + 'T00:00:00Z')
  d.setUTCDate(d.getUTCDate() + n)
  return d.toISOString().slice(0, 10)
}
