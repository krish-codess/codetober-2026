import { useEffect, useRef, useState } from 'react'
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type Page, type QueueStatus, type Reservation } from './api'
import { Checkout } from './Checkout'
import { Queue } from './Queue'
import { AvailabilityTable, TicketPicker } from './Tickets'
import { clock, clockOffsetMs, useServerNow } from './time'
import './styles.css'

// Caching policy, in one place:
//  - event: immutable for the life of the page                     -> fetched once
//  - availability: server caches 1 s; we poll every 2 s            -> never refetched on focus/mount in between
//  - queue status: polled at the interval the server asks for      -> pollAfterMs
//  - my reservations: only changes when *we* change it             -> fetched once, then updated from mutation results
const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: false, staleTime: Infinity } },
})

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <main>
        <Root />
      </main>
    </QueryClientProvider>
  )
}

function Root() {
  const requested = new URLSearchParams(window.location.search).get('event')
  const events = useQuery({ queryKey: ['events'], queryFn: api.events, enabled: !requested })
  if (requested) return <Sale eventId={requested} />
  if (events.isPending) return <Loading label="Loading events" />
  if (events.isError) return <Failed error={events.error} onRetry={() => events.refetch()} busy={events.isFetching} />
  if (events.data.items.length === 0) {
    return (
      <>
        <h1>The Last Ticket</h1>
        <div className="notice">Nothing is on sale right now. Check back later.</div>
      </>
    )
  }
  if (events.data.items.length === 1) return <Sale eventId={events.data.items[0]!.id} />
  return (
    <>
      <h1>The Last Ticket</h1>
      <ul>
        {events.data.items.map((e) => (
          <li key={e.id}>
            <a href={`?event=${e.id}`}>{e.name}</a> <span className="muted">on sale {new Date(e.onSaleAt).toLocaleString()}</span>
          </li>
        ))}
      </ul>
    </>
  )
}

function Loading({ label }: { label: string }) {
  return (
    <div className="card" role="status" aria-label={label}>
      <div className="skeleton" style={{ width: '60%' }} /><div className="skeleton" /><div className="skeleton" style={{ width: '80%' }} />
    </div>
  )
}

function Failed({ error, onRetry, busy }: { error: Error; onRetry: () => void; busy: boolean }) {
  const notFound = error instanceof ApiError && error.status === 404
  return (
    <div className="card">
      <div className="notice bad" role="alert">
        <strong>✕ {notFound ? 'That event does not exist' : 'We could not load this page'}</strong>
        {error.message}
      </div>
      {!notFound && <button onClick={onRetry} disabled={busy}>{busy ? 'Trying…' : 'Try again'}</button>}
    </div>
  )
}

type QueueResult = QueueStatus & { rejoined?: boolean }
const joinedKey = (eventId: string) => `lastticket.joined.${eventId}`

function Sale({ eventId }: { eventId: string }) {
  const client = useQueryClient()
  const event = useQuery({
    queryKey: ['event', eventId],
    // The clock offset is measured once, when the response arrives.
    queryFn: async () => {
      const e = await api.event(eventId)
      return { ...e, clockOffsetMs: clockOffsetMs(e.serverTime) }
    },
  })
  const availability = useQuery({
    queryKey: ['availability', eventId],
    queryFn: () => api.availability(eventId),
    refetchInterval: 2000,
    enabled: event.isSuccess,
  })
  const reservations = useQuery({ queryKey: ['reservations'], queryFn: api.myReservations, enabled: event.isSuccess })
  const queue = useQuery<QueueResult, ApiError>({
    queryKey: ['queue', eventId],
    queryFn: async () => {
      const status = await api.queueStatus(eventId)
      // We were in line and now we are not (turn lapsed, or the queue was reset): get back in line rather than
      // leaving the user looking at a dead page.
      if (status.state === 'NOT_IN_QUEUE' && sessionStorage.getItem(joinedKey(eventId))) {
        return { ...(await api.joinQueue(eventId)), rejoined: true }
      }
      return status
    },
    enabled: event.isSuccess,
    refetchInterval: (q) => (q.state.error?.retryAfterSeconds ?? 0) * 1000 || q.state.data?.pollAfterMs || 5000,
  })

  const [joining, setJoining] = useState(false)
  const [joinError, setJoinError] = useState<ApiError | null>(null)
  const [confirmed, setConfirmed] = useState<Reservation | null>(null)

  const held = reservations.data?.items.find((r) => r.eventId === eventId && r.status === 'HELD')
  const state = queue.data?.state
  const step = confirmed ? 'done' : held ? 'checkout' : state === 'ADMITTED' ? 'pick' : state === 'LOBBY' || state === 'QUEUED' ? 'queue' : 'landing'

  // Move focus to the new step's heading so keyboard and screen-reader users are not left on a control that vanished.
  const heading = useRef<HTMLHeadingElement>(null)
  const firstStep = useRef(true)
  useEffect(() => {
    if (firstStep.current) firstStep.current = false
    else heading.current?.focus()
  }, [step])

  if (event.isPending) return <Loading label="Loading event" />
  if (event.isError) return <Failed error={event.error} onRetry={() => event.refetch()} busy={event.isFetching} />

  const offset = event.data.clockOffsetMs
  const setReservation = (r: Reservation) =>
    client.setQueryData<Page<Reservation>>(['reservations'], (old) => ({
      items: [r, ...(old?.items ?? []).filter((x) => x.id !== r.id)],
    }))

  async function join() {
    setJoining(true)
    setJoinError(null)
    try {
      const status = await api.joinQueue(eventId)
      sessionStorage.setItem(joinedKey(eventId), '1')
      client.setQueryData(['queue', eventId], status)
    } catch (e) {
      setJoinError(e instanceof ApiError ? e : new ApiError(0, 'NETWORK', 'Something went wrong.'))
    } finally {
      setJoining(false)
    }
  }

  const sectionOf = (r: Reservation) => availability.data?.sections.find((s) => s.inventoryId === r.inventoryId)
  const availableNow = availability.data?.sections.reduce((n, s) => n + s.available, 0)

  return (
    <>
      <header>
        <h1>{event.data.name}</h1>
        <SaleClock onSaleAt={event.data.onSaleAt} offset={offset} />
      </header>

      {step === 'done' && confirmed && (
        <section className="card" aria-labelledby="step">
          <h2 id="step" ref={heading} tabIndex={-1}>You are going!</h2>
          <div className="notice ok" role="status">
            <strong>✓ Order confirmed</strong>
            {confirmed.quantity} × {sectionOf(confirmed)?.ticketType} · {sectionOf(confirmed)?.section}. Reference {confirmed.id.slice(0, 8).toUpperCase()}.
          </div>
        </section>
      )}

      {step === 'checkout' && held && (
        <Checkout
          key={held.id}
          reservation={held}
          section={sectionOf(held)}
          holdSeconds={event.data.holdSeconds}
          headingRef={heading}
          onConfirmed={(r) => {
            setReservation(r)
            setConfirmed(r)
          }}
          onGone={() => reservations.refetch()}
        />
      )}

      {step === 'pick' && queue.data?.admissionToken && (
        <TicketPicker
          availability={availability.data}
          maxPerUser={event.data.maxPerUser}
          admissionToken={queue.data.admissionToken}
          headingRef={heading}
          onHeld={setReservation}
          onStale={() => {
            queue.refetch()
            reservations.refetch()
          }}
        />
      )}

      {step === 'queue' && queue.data && (
        <Queue
          status={queue.data}
          availableNow={availableNow}
          pollError={queue.error}
          rejoined={queue.data.rejoined === true}
          clockOffsetMs={offset}
          headingRef={heading}
        />
      )}

      {step === 'landing' && (
        <section className="card" aria-labelledby="step">
          <h2 id="step" ref={heading} tabIndex={-1}>Get in line</h2>
          {queue.isPending && !queue.isError ? (
            <div role="status" aria-label="Checking your place in line"><div className="skeleton" style={{ width: '50%' }} /></div>
          ) : (
            <>
              <p>
                Join before the sale opens and you get a random place in line when it does. Join after, and you go to the
                back. Either way, refreshing never helps and never hurts.
              </p>
              {(joinError ?? queue.error) && (
                <div className="notice bad" role="alert">
                  <strong>✕ The queue is not answering</strong>
                  {(joinError ?? queue.error)!.message}
                </div>
              )}
              <button onClick={join} disabled={joining}>{joining ? 'Joining…' : 'Join the queue'}</button>
            </>
          )}
        </section>
      )}

      <h2 style={{ marginTop: '1.5rem' }}>Availability</h2>
      <AvailabilityTable data={availability.data} error={availability.isError} loading={availability.isPending && !availability.isError} />
    </>
  )
}

function SaleClock({ onSaleAt, offset }: { onSaleAt: string; offset: number }) {
  const now = useServerNow(offset, 1000)
  const seconds = (Date.parse(onSaleAt) - now) / 1000
  return (
    <p className="muted">
      {seconds > 0 ? <>Sale opens in <strong>{clock(seconds)}</strong> ({new Date(onSaleAt).toLocaleTimeString()})</> : 'On sale now'}
    </p>
  )
}
