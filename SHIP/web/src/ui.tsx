import { useEffect, useState } from 'react'
import type { ApiError, Polled, RunState } from './api'

export const n = (v: number) => v.toLocaleString('en')

// Meaning is carried by the glyph and the word; colour only repeats it.
const STATE: Record<RunState, { glyph: string; tone: string; label: string }> = {
  pending: { glyph: '○', tone: 'idle', label: 'Pending' },
  expanding: { glyph: '◐', tone: 'busy', label: 'Expanding' },
  expanded: { glyph: '⇄', tone: 'busy', label: 'Expanded: both versions live' },
  contracting: { glyph: '◑', tone: 'busy', label: 'Contracting' },
  completed: { glyph: '✓', tone: 'good', label: 'Completed' },
  reverting: { glyph: '↺', tone: 'warn', label: 'Reverting' },
  reverted: { glyph: '↺', tone: 'warn', label: 'Reverted' },
  failed: { glyph: '✕', tone: 'bad', label: 'Failed' },
}

export function StateBadge({ state, label }: { state: RunState; label?: string }) {
  const s = STATE[state]
  return (
    <span className={`badge ${s.tone}`}>
      <span aria-hidden="true">{s.glyph}</span> {label ?? s.label}
    </span>
  )
}

export function ErrorNote({ error, retry }: { error: ApiError; retry?: () => void }) {
  return (
    <div role="alert" className="note bad">
      <p>
        <strong>{error.status === 403 ? 'Not allowed. ' : error.status === 409 ? 'Refused. ' : 'Something went wrong. '}</strong>
        {error.message}
      </p>
      {error.details.length > 0 && (
        <ul>
          {error.details.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
      )}
      {retry && <button onClick={retry}>Try again</button>}
    </div>
  )
}

/** Shown when a view is displaying its last good answer because refreshing it keeps failing. */
export function Stale({ of }: { of: Polled<unknown> }) {
  const [, tick] = useState(0)
  useEffect(() => {
    if (!of.stale) return
    const t = setInterval(() => tick((x) => x + 1), 1000)
    return () => clearInterval(t)
  }, [of.stale])
  if (!of.stale || !of.updatedAt) return null
  return (
    <p role="status" className="note warn">
      <strong>Out of date.</strong> Last updated {Math.round((Date.now() - of.updatedAt) / 1000)} s ago; {of.error?.message ?? 'refreshing failed.'}
    </p>
  )
}
