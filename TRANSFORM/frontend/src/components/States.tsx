import type { ReactNode } from 'react'
import type { ApiError, Freshness } from '../lib/api'
import { day } from '../lib/format'

/** Determinate progress: "Loading 2 of 4 …" — never an indefinite spinner. */
export function Progress({ done, total, what }: { done: number; total: number; what: string }) {
  return (
    <div className="state loading" role="status" aria-live="polite">
      <progress max={total} value={done} aria-label={`Loading ${what}`} />
      <span>
        Loading {what}: {done} of {total}
      </span>
    </div>
  )
}

export function ErrorPanel({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  const hint =
    error.code === 'database_unavailable' || error.status === 0
      ? 'The service is temporarily unavailable. Your last data stays on screen; retry in a moment.'
      : error.status >= 500
        ? 'Something went wrong on our side.'
        : 'The request could not be completed.'
  return (
    <div className="state error" role="alert">
      <strong>{hint}</strong>
      <p>
        {error.message}
        {error.requestId && <span className="muted"> (request id {error.requestId})</span>}
      </p>
      <button type="button" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="state empty" role="status">
      {children}
    </div>
  )
}

export function StaleBanner({ freshness }: { freshness: Freshness | undefined }) {
  if (!freshness?.stale) return null
  return (
    <div className="banner stale" role="status">
      <span aria-hidden="true">⏱</span> Data is stale:{' '}
      {freshness.last_day ? (
        <>
          newest published day is {day(freshness.last_day)} ({Math.round(freshness.age_hours ?? 0)} h old).
        </>
      ) : (
        'nothing has been published yet.'
      )}{' '}
      Values shown are the last known, not current.
    </div>
  )
}

export function PartialBanner({ partial, missing, revised }: { partial: number; missing: number; revised: number }) {
  if (!partial && !missing && !revised) return null
  return (
    <div className="banner partial" role="status">
      <span aria-hidden="true">◐</span>{' '}
      {partial > 0 && <>{partial} day(s) with partial coverage (dashed). </>}
      {missing > 0 && <>{missing} day(s) without enough data to publish (gaps). </>}
      {revised > 0 && <>{revised} value(s) were revised after first publication.</>}
    </div>
  )
}
