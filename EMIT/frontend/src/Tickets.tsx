import { useState } from 'react'
import { api, ApiError, type Availability, type Reservation } from './api'
import { money } from './time'

/** Read-only availability, shown at every step. Text + tag carry the state; colour only reinforces it. */
export function AvailabilityTable({ data, error, loading }: { data?: Availability; error: boolean; loading: boolean }) {
  if (loading) {
    return (
      <div className="card" aria-busy="true" aria-label="Loading availability">
        <div className="skeleton" /><div className="skeleton" /><div className="skeleton" />
      </div>
    )
  }
  if (!data) {
    return (
      <div className="notice bad" role="alert">
        <strong>✕ Availability could not be loaded</strong>
        We will keep trying. Your place in line is not affected.
      </div>
    )
  }
  if (data.sections.length === 0) {
    return <div className="notice">No tickets have been put on sale for this event yet.</div>
  }
  return (
    <div className="card">
      <table>
        <caption className="sr-only">Tickets available by section</caption>
        <thead>
          <tr><th scope="col">Section</th><th scope="col" className="num">Price</th><th scope="col" className="num">Available</th></tr>
        </thead>
        <tbody>
          {data.sections.map((s) => (
            <tr key={s.inventoryId}>
              <th scope="row" style={{ color: 'inherit', fontSize: 'inherit' }}>{s.ticketType} · {s.section}</th>
              <td className="num">{money(s.priceCents)}</td>
              <td className="num">
                {s.available === 0 ? <span className="tag bad">Sold out</span>
                  : s.available <= 10 ? <span className="tag warn">Only {s.available} left</span>
                    : `${s.available.toLocaleString()} of ${s.total.toLocaleString()}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && <p className="muted" role="status">⚠ Could not refresh just now; these numbers may be a few seconds old.</p>}
    </div>
  )
}

interface PickerProps {
  availability?: Availability
  maxPerUser: number
  admissionToken: string
  headingRef: React.Ref<HTMLHeadingElement>
  onHeld: (r: Reservation) => void
  /** Something we believed is no longer true (admission lapsed, a hold already exists): re-read server state. */
  onStale: () => void
}

/** It is your turn: choose a section and quantity and place a hold. */
export function TicketPicker({ availability, maxPerUser, admissionToken, headingRef, onHeld, onStale }: PickerProps) {
  const sections = availability?.sections ?? []
  const [chosen, setChosen] = useState<string | null>(null)
  const [quantity, setQuantity] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  // One key per purchase intent: a retry after a timeout cannot create a second hold. Rotated after a definite refusal.
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID())

  const selected = sections.find((s) => s.inventoryId === chosen && s.available > 0) ?? sections.find((s) => s.available > 0)
  const max = Math.min(maxPerUser, selected?.available ?? 0)
  const qty = Math.min(quantity, Math.max(max, 1))

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!selected) return
    setBusy(true)
    setError(null)
    try {
      onHeld(await api.reserve(selected.inventoryId, qty, admissionToken, idempotencyKey))
    } catch (raw) {
      const err = raw instanceof ApiError ? raw : new ApiError(0, 'NETWORK', 'Something went wrong.')
      setError(err)
      if (!err.transient) setIdempotencyKey(crypto.randomUUID())
      if (err.code === 'NOT_ADMITTED' || err.code === 'ALREADY_HOLDING') onStale()
    } finally {
      setBusy(false)
    }
  }

  if (availability && !selected) {
    return (
      <section className="card" aria-labelledby="step">
        <h2 id="step" ref={headingRef} tabIndex={-1}>It is your turn, but nothing is available</h2>
        <div className="notice bad">
          <strong>✕ Everything is sold or in someone’s basket</strong>
          Unpaid baskets expire after a couple of minutes and come back on sale. This page updates by itself; you can
          wait here while your turn lasts.
        </div>
      </section>
    )
  }

  return (
    <section className="card" aria-labelledby="step">
      <h2 id="step" ref={headingRef} tabIndex={-1}>It is your turn</h2>
      <form onSubmit={submit}>
        <fieldset disabled={busy}>
          <legend>Section</legend>
          {sections.map((s) => (
            <label className="option" key={s.inventoryId}>
              <input type="radio" name="section" value={s.inventoryId} disabled={s.available === 0}
                checked={selected?.inventoryId === s.inventoryId} onChange={() => setChosen(s.inventoryId)} />
              <span>{s.ticketType} · {s.section}</span>
              <span>{s.available === 0 ? 'Sold out' : money(s.priceCents)}</span>
            </label>
          ))}
        </fieldset>
        <p>
          <label htmlFor="qty">Tickets </label>
          <select id="qty" value={qty} onChange={(e) => setQuantity(Number(e.target.value))} disabled={busy}>
            {Array.from({ length: Math.max(max, 1) }, (_, i) => i + 1).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>{' '}
          <span className="muted">Limit {maxPerUser} per person.</span>
        </p>
        {error && (
          <div className="notice bad" role="alert">
            <strong>✕ {error.code === 'SOLD_OUT' ? 'Someone got there first' : error.transient ? 'That did not go through' : 'We could not hold those tickets'}</strong>
            {error.message} {error.transient && 'Nothing was reserved; you can try again.'}
          </div>
        )}
        <button type="submit" disabled={busy || !selected}>
          {busy ? 'Holding your tickets…' : selected ? `Hold ${qty} for ${money(selected.priceCents * qty)}` : 'Loading…'}
        </button>
      </form>
    </section>
  )
}
