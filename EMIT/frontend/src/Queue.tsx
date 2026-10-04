import { useState } from 'react'
import type { ApiError, QueueStatus } from './api'
import { clock, roughWait, useServerNow } from './time'

interface Props {
  status: QueueStatus
  /** Tickets available right now across all sections; undefined while unknown. */
  availableNow?: number
  /** Set when the latest poll failed: `status` is then the last known one. */
  pollError: ApiError | null
  rejoined: boolean
  clockOffsetMs: number
  headingRef: React.Ref<HTMLHeadingElement>
}

/** Queue position with an honest wait estimate. Only LOBBY and QUEUED are rendered here. */
export function Queue({ status, availableNow, pollError, rejoined, clockOffsetMs, headingRef }: Props) {
  const now = useServerNow(clockOffsetMs, 500)
  const position = status.position ?? 0
  // Furthest back we have been: makes the bar real progress through the line rather than decoration.
  const [start, setStart] = useState(position)
  if (position > start) setStart(position)

  const stale = pollError && (
    <div className="notice warn" role="status">
      <strong>⚠ Lost contact with the queue</strong>
      You keep your place. Showing the last position we heard; retrying automatically.
    </div>
  )

  if (status.state === 'LOBBY') {
    const untilOpen = (Date.parse(status.onSaleAt) - now) / 1000
    return (
      <section className="card" aria-labelledby="step">
        <h2 id="step" ref={headingRef} tabIndex={-1}>You are in the lobby</h2>
        {stale}
        <p className="muted" id="opens-label">Sale opens in</p>
        <div className="big" role="timer" aria-labelledby="opens-label">{clock(untilOpen)}</div>
        <p>
          When the sale opens, everyone in the lobby is given a <strong>random place in line</strong>. Arriving earlier or
          refreshing does not improve it, so there is nothing to do but wait.
        </p>
      </section>
    )
  }

  const ahead = position - 1
  const soldOutNow = availableNow === 0
  return (
    <section className="card" aria-labelledby="step">
      <h2 id="step" ref={headingRef} tabIndex={-1}>You are in line</h2>
      {stale}
      {rejoined && (
        <div className="notice warn" role="status">
          <strong>↻ You were put back in line</strong>
          Your turn lapsed or the queue was reset, so we rejoined it for you.
        </div>
      )}
      <p className="muted">Your position</p>
      {/* Polite: announced when it changes, without interrupting. */}
      <div className="big" aria-live="polite" aria-atomic="true" data-testid="position">{position.toLocaleString()}</div>
      <progress max={Math.max(start, 1)} value={Math.max(start, 1) - ahead} aria-label="Progress through the line" />
      <p>
        {ahead === 0 ? 'You are next.' : `${ahead.toLocaleString()} ${ahead === 1 ? 'person' : 'people'} ahead of you.`}{' '}
        Estimated wait: <strong>{roughWait(status.estimatedWaitSeconds ?? 0)}</strong>.
      </p>
      <p className="muted">
        That estimate is how long until it is your turn at the current admission rate. It is not a promise of tickets.
      </p>
      {availableNow !== undefined && (
        <div className={`notice ${soldOutNow ? 'bad' : ''}`}>
          <strong>{soldOutNow ? '✕ Nothing available right now' : `${availableNow.toLocaleString()} tickets available right now`}</strong>
          {soldOutNow
            ? 'Everything is sold or in someone’s basket. Unpaid baskets expire and come back on sale; you keep your place if you wait.'
            : ahead > availableNow
              ? 'There are more people ahead of you than tickets left, so they may sell out before your turn.'
              : 'There are fewer people ahead of you than tickets left.'}
        </div>
      )}
      <p className="muted">Keep this page open. Refreshing will not change your position.</p>
    </section>
  )
}
