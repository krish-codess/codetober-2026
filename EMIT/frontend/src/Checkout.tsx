import { useState } from 'react'
import { api, ApiError, type Reservation, type Section } from './api'
import { clock, clockOffsetMs, money, useServerNow } from './time'

interface Props {
  reservation: Reservation
  section?: Section
  holdSeconds: number
  headingRef: React.Ref<HTMLHeadingElement>
  onConfirmed: (r: Reservation) => void
  /** The hold is gone (expired or released): go back to choosing tickets. */
  onGone: () => void
}

/** Checkout with a visible hold countdown, driven by the server's clock and the hold's hard deadline. */
export function Checkout({ reservation, section, holdSeconds, headingRef, onConfirmed, onGone }: Props) {
  const [offset] = useState(() => clockOffsetMs(reservation.serverTime))
  const now = useServerNow(offset)
  const [busy, setBusy] = useState<'pay' | 'release' | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [serverSaysExpired, setServerSaysExpired] = useState(false)

  const remaining = (Date.parse(reservation.expiresAt) - now) / 1000
  const expired = serverSaysExpired || remaining <= 0

  async function run(kind: 'pay' | 'release') {
    setBusy(kind)
    setError(null)
    try {
      if (kind === 'pay') onConfirmed(await api.confirm(reservation.id))
      else {
        await api.release(reservation.id)
        onGone()
      }
    } catch (e) {
      const err = e instanceof ApiError ? e : new ApiError(0, 'NETWORK', 'Something went wrong.')
      if (err.code === 'HOLD_EXPIRED') setServerSaysExpired(true)
      else setError(err)
    } finally {
      setBusy(null)
    }
  }

  if (expired) {
    return (
      <section className="card" aria-labelledby="step">
        <h2 id="step" ref={headingRef} tabIndex={-1}>Your hold expired</h2>
        <div className="notice bad" role="alert">
          <strong>⏱ Time ran out.</strong>
          The tickets went back on sale and you have not been charged.
        </div>
        <button onClick={onGone}>Choose tickets again</button>
      </section>
    )
  }

  // Spoken at thresholds only: a live region that changed every second would be unusable with a screen reader.
  const spoken = remaining <= 10 ? '10 seconds left to pay.' : remaining <= 30 ? '30 seconds left to pay.' : remaining <= 60 ? 'One minute left to pay.' : ''
  const total = section ? money(section.priceCents * reservation.quantity) : null

  return (
    <section className="card" aria-labelledby="step">
      <h2 id="step" ref={headingRef} tabIndex={-1}>Checkout</h2>
      <p>
        <strong>{reservation.quantity} × {section ? `${section.ticketType} · ${section.section}` : 'tickets'}</strong>
        {total && <> — {total}</>}
      </p>
      <p className="muted" id="hold-label">These tickets are held for you for</p>
      <div className="big" role="timer" aria-labelledby="hold-label" data-testid="countdown">{clock(remaining)}</div>
      <progress max={holdSeconds} value={Math.min(holdSeconds, remaining)} aria-hidden="true" />
      {remaining <= 30 && (
        <p><span className="tag warn">⚠ Almost out of time</span></p>
      )}
      <p className="sr-only" aria-live="assertive">{spoken}</p>

      {error && (
        <div className="notice bad" role="alert">
          <strong>✕ {error.transient ? 'That did not go through.' : 'We could not complete that.'}</strong>
          {error.message} {error.transient && 'You have not been charged; it is safe to try again.'}
        </div>
      )}
      <div className="row">
        <button onClick={() => run('pay')} disabled={busy !== null}>
          {busy === 'pay' ? 'Paying…' : `Pay${total ? ` ${total}` : ''}`}
        </button>
        <button className="secondary" onClick={() => run('release')} disabled={busy !== null}>
          {busy === 'release' ? 'Releasing…' : 'Release tickets'}
        </button>
      </div>
      <p className="muted">Demo checkout: no card is taken. If you close this page the hold simply expires.</p>
    </section>
  )
}
