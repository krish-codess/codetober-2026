// Typed client for the backend. One place that knows about tokens, error shape and the server clock.

export interface EventView {
  id: string
  name: string
  onSaleAt: string
  holdSeconds: number
  maxPerUser: number
  admissionRatePerSec: number
  serverTime: string
}
export interface Section {
  inventoryId: string
  ticketType: string
  section: string
  priceCents: number
  available: number
  total: number
}
export interface Availability {
  eventId: string
  asOf: string
  sections: Section[]
}
export type QueueState = 'LOBBY' | 'QUEUED' | 'ADMITTED' | 'NOT_IN_QUEUE'
export interface QueueStatus {
  state: QueueState
  position?: number
  estimatedWaitSeconds?: number
  admissionToken?: string
  admissionExpiresAt?: string
  onSaleAt: string
  pollAfterMs: number
}
export interface Reservation {
  id: string
  inventoryId: string
  eventId: string
  quantity: number
  status: 'HELD' | 'CONFIRMED' | 'EXPIRED' | 'RELEASED'
  expiresAt: string
  createdAt: string
  serverTime: string
}
export interface Page<T> {
  items: T[]
  next?: string
}

/** An error response in the API's problem+json shape, or a network failure (status 0, code NETWORK). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    detail: string,
    readonly retryAfterSeconds?: number,
    readonly correlationId?: string,
  ) {
    super(detail)
  }
  /** Worth retrying without the user changing anything. */
  get transient() {
    return this.status === 0 || this.status === 503 || this.status === 502 || this.status === 504
  }
}

const SESSION_KEY = 'lastticket.session'
let sessionPromise: Promise<string> | null = null

async function raw<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, init)
  } catch {
    throw new ApiError(0, 'NETWORK', 'Could not reach the server. Check your connection.')
  }
  if (res.ok) return (await res.json()) as T
  const problem = (await res.json().catch(() => ({}))) as { code?: string; detail?: string; correlationId?: string }
  const retryAfter = Number(res.headers.get('Retry-After')) || undefined
  throw new ApiError(
    res.status,
    problem.code ?? 'ERROR',
    problem.detail ?? `The server answered ${res.status}.`,
    retryAfter,
    problem.correlationId,
  )
}

/** One anonymous session per browser tab session; survives refresh so a refresh never costs you your place. */
function session(): Promise<string> {
  const stored = sessionStorage.getItem(SESSION_KEY)
  if (stored) return Promise.resolve(stored)
  sessionPromise ??= raw<{ token: string }>('/api/sessions', { method: 'POST' }).then(
    (s) => {
      sessionStorage.setItem(SESSION_KEY, s.token)
      return s.token
    },
    (e) => {
      sessionPromise = null
      throw e
    },
  )
  return sessionPromise
}

async function authed<T>(path: string, init: RequestInit = {}, retried = false): Promise<T> {
  const token = await session()
  try {
    return await raw<T>(path, { ...init, headers: { ...init.headers, Authorization: `Bearer ${token}` } })
  } catch (e) {
    // Session expired (they last 6 h): start a new one, once. The user re-queues; nothing else can be done honestly.
    if (e instanceof ApiError && e.status === 401 && !retried) {
      sessionStorage.removeItem(SESSION_KEY)
      sessionPromise = null
      return authed<T>(path, init, true)
    }
    throw e
  }
}

export const api = {
  events: () => raw<Page<EventView>>('/api/events?limit=50'),
  event: (id: string) => raw<EventView>(`/api/events/${id}`),
  availability: (id: string) => raw<Availability>(`/api/events/${id}/availability`),
  joinQueue: (id: string) => authed<QueueStatus>(`/api/events/${id}/queue`, { method: 'POST' }),
  queueStatus: (id: string) => authed<QueueStatus>(`/api/events/${id}/queue`),
  myReservations: () => authed<Page<Reservation>>('/api/reservations?limit=20'),
  reserve: (inventoryId: string, quantity: number, admissionToken: string, idempotencyKey: string) =>
    authed<Reservation>('/api/reservations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admission-Token': admissionToken, 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ inventoryId, quantity }),
    }),
  confirm: (id: string) => authed<Reservation>(`/api/reservations/${id}/confirm`, { method: 'POST' }),
  release: (id: string) => authed<Reservation>(`/api/reservations/${id}`, { method: 'DELETE' }),
}
