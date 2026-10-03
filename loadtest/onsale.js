// The on-sale, as a load test: USERS people, 1,200 tickets, one hard start time.
//
//   docker compose --profile loadtest run --rm k6                       # defaults below
//   docker compose --profile loadtest run --rm -e USERS=10000 -e VUS=2000 k6
//
// Every simulated user does what the browser does: session -> join the waiting room (most of them before the sale
// opens) -> poll at the interval the server asks for -> on admission, hold tickets -> 70% pay, 30% walk away and let
// the hold expire. A second scenario hammers the public availability endpoint the whole time.
//
// The run FAILS (non-zero exit) if anything was oversold, if the event stream and the snapshot disagree, or if the
// latency budget for the critical path (placing a hold) is blown. Results land in loadtest/results/.
import http from 'k6/http'
import { check, sleep } from 'k6'
import { Counter, Trend } from 'k6/metrics'

const BASE = __ENV.BASE_URL || 'http://localhost:8080'
const ADMIN = { 'X-Admin-Key': __ENV.ADMIN_API_KEY || 'change-me-admin', 'Content-Type': 'application/json' }
const USERS = Number(__ENV.USERS || 4000)
const VUS = Number(__ENV.VUS || 1000) // each VU drives USERS / VUS users concurrently-ish (keeps k6's memory sane)
const PER_VU = Math.ceil(USERS / VUS)
const LOBBY_SECONDS = Number(__ENV.LOBBY_SECONDS || 20)
const HOLD_SECONDS = Number(__ENV.HOLD_SECONDS || 15)
const ADMISSION_RATE = Number(__ENV.ADMISSION_RATE || 100)
const READ_RPS = Number(__ENV.READ_RPS || 200)
const MAX_SECONDS = LOBBY_SECONDS + Math.ceil(USERS / ADMISSION_RATE) + HOLD_SECONDS + 45

const SECTIONS = [
  { ticketType: 'GA', section: 'FLOOR', priceCents: 9500, total: 400, demand: 40 },
  { ticketType: 'GA', section: 'LOWER', priceCents: 7500, total: 300, demand: 20 },
  { ticketType: 'GA', section: 'UPPER', priceCents: 4500, total: 380, demand: 12 },
  { ticketType: 'VIP', section: 'FLOOR', priceCents: 22000, total: 80, demand: 18 },
  { ticketType: 'VIP', section: 'BOX', priceCents: 32000, total: 40, demand: 10 },
]
const STOCK = SECTIONS.reduce((n, s) => n + s.total, 0)

const reserveLatency = new Trend('reserve_latency', true) // the critical path
const confirmLatency = new Trend('confirm_latency', true)
const queueLatency = new Trend('queue_poll_latency', true)
const availabilityLatency = new Trend('availability_latency', true)
const outcomes = {
  held: new Counter('reserve_held'),
  soldOut: new Counter('reserve_sold_out'),
  contention: new Counter('reserve_contention_503'),
  other: new Counter('reserve_other_error'),
  confirmed: new Counter('confirm_ok'),
  abandoned: new Counter('holds_abandoned'),
  gaveUp: new Counter('users_left_empty_handed'),
  unexpected5xx: new Counter('unexpected_5xx'),
}

export const options = {
  discardResponseBodies: false,
  scenarios: {
    // Instances are pre-warmed before an on-sale (JIT, connection pools): a cold JVM is ~20x slower for its first seconds.
    warmup: { executor: 'constant-vus', exec: 'warm', vus: 20, duration: `${Math.max(5, LOBBY_SECONDS - 5)}s` },
    buyers: { executor: 'per-vu-iterations', vus: VUS, iterations: 1, maxDuration: `${MAX_SECONDS}s` },
    browsers: {
      executor: 'constant-arrival-rate', exec: 'browse', rate: READ_RPS, timeUnit: '1s',
      duration: `${MAX_SECONDS - 10}s`, preAllocatedVUs: 50, maxVUs: 300,
    },
  },
  thresholds: {
    // Latency budget for the critical path, measured at the client, at T0 load.
    reserve_latency: ['p(50)<100', 'p(95)<400', 'p(99)<800'],
    // Reads must stay fast while writers fight over the same rows.
    availability_latency: ['p(95)<150', 'p(99)<400'],
    unexpected_5xx: ['count==0'],
    'checks{invariant:true}': ['rate==1'],
  },
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
}

export function setup() {
  const id = uuid()
  const onSaleAt = new Date(Date.now() + LOBBY_SECONDS * 1000).toISOString()
  const res = http.put(`${BASE}/api/admin/events/${id}`, JSON.stringify({
    name: `k6 on-sale ${USERS} users`, onSaleAt, holdSeconds: HOLD_SECONDS, maxPerUser: 4, admissionRatePerSec: ADMISSION_RATE,
    inventory: SECTIONS.map(({ ticketType, section, priceCents, total }) => ({ ticketType, section, priceCents, total })),
  }), { headers: ADMIN })
  if (res.status !== 201) throw new Error(`could not create event: ${res.status} ${res.body}`)
  const sections = http.get(`${BASE}/api/events/${id}/availability`).json('sections')
  // A separate, already-open event with effectively unlimited stock, for the warm-up scenario only.
  const warmId = uuid()
  http.put(`${BASE}/api/admin/events/${warmId}`, JSON.stringify({
    name: 'k6 warm-up', onSaleAt: new Date(Date.now() - 60000).toISOString(), holdSeconds: 30, maxPerUser: 4, admissionRatePerSec: 1000,
    inventory: SECTIONS.map(({ ticketType, section, priceCents }) => ({ ticketType, section, priceCents, total: 1000000 })),
  }), { headers: ADMIN })
  const warmSections = http.get(`${BASE}/api/events/${warmId}/availability`).json('sections')
  return { id, onSaleAtMs: Date.parse(onSaleAt), sections, warmId, warmSections }
}

export function browse(data) {
  const res = http.get(`${BASE}/api/events/${data.id}/availability`, { tags: { name: 'availability' } })
  availabilityLatency.add(res.timings.duration)
  if (res.status >= 500) outcomes.unexpected5xx.add(1)
}

// The full journey against the warm-up event. Not recorded in the custom latency metrics.
export function warm(data) {
  const headers = { Authorization: `Bearer ${http.post(`${BASE}/api/sessions`).json('token')}` }
  let s = http.post(`${BASE}/api/events/${data.warmId}/queue`, null, { headers }).json()
  for (let i = 0; i < 10 && s.state !== 'ADMITTED'; i++) {
    sleep(0.3)
    s = http.get(`${BASE}/api/events/${data.warmId}/queue`, { headers }).json()
  }
  http.get(`${BASE}/api/events/${data.warmId}/availability`)
  if (s.state !== 'ADMITTED') return
  const section = data.warmSections[Math.floor(Math.random() * data.warmSections.length)]
  const res = http.post(`${BASE}/api/reservations`, JSON.stringify({ inventoryId: section.inventoryId, quantity: 1 }), {
    headers: Object.assign({ 'Content-Type': 'application/json', 'X-Admission-Token': s.admissionToken, 'Idempotency-Key': uuid() }, headers),
  })
  if (res.status === 201) http.post(`${BASE}/api/reservations/${res.json('id')}/confirm`, null, { headers })
}

// One VU drives PER_VU users as small state machines, always servicing whichever is due next.
export default function (data) {
  const users = []
  for (let i = 0; i < PER_VU; i++) {
    const token = http.post(`${BASE}/api/sessions`, null, { tags: { name: 'session' } }).json('token')
    // 60% are already in the lobby when the sale opens; the rest pile in during the first 5 seconds.
    const joinAt = Math.random() < 0.6 ? Date.now() + Math.random() * Math.max(0, data.onSaleAtMs - Date.now() - 2000)
      : data.onSaleAtMs + Math.random() * 5000
    users.push({ headers: { Authorization: `Bearer ${token}` }, state: 'NEW', dueAt: joinAt, tries: 0 })
  }
  let active = users.length
  while (active > 0) {
    let u = null
    for (const x of users) if (x.state !== 'DONE' && (u === null || x.dueAt < u.dueAt)) u = x
    const wait = u.dueAt - Date.now()
    if (wait > 0) sleep(wait / 1000)
    step(u, data)
    if (u.state === 'DONE') active--
  }
}

function step(u, data) {
  if (u.state === 'NEW' || u.state === 'WAITING') {
    const res = u.state === 'NEW'
      ? http.post(`${BASE}/api/events/${data.id}/queue`, null, { headers: u.headers, tags: { name: 'queue_join' } })
      : http.get(`${BASE}/api/events/${data.id}/queue`, { headers: u.headers, tags: { name: 'queue_poll' } })
    queueLatency.add(res.timings.duration)
    if (res.status !== 200) {
      if (res.status >= 500 && res.status !== 503) outcomes.unexpected5xx.add(1)
      u.dueAt = Date.now() + 2000
      return
    }
    const s = res.json()
    if (s.state === 'ADMITTED') {
      u.admission = s.admissionToken
      u.state = 'ADMITTED'
      u.dueAt = Date.now() + 200 + Math.random() * 1500 // looking at the page
    } else {
      u.state = s.state === 'NOT_IN_QUEUE' ? 'NEW' : 'WAITING'
      u.dueAt = Date.now() + (s.pollAfterMs || 1000)
    }
    return
  }
  if (u.state === 'ADMITTED') {
    const wanted = u.section || weighted(data.sections)
    u.key = u.key || uuid()
    const res = http.post(`${BASE}/api/reservations`, JSON.stringify({ inventoryId: wanted.inventoryId, quantity: u.qty || (u.qty = quantity()) }), {
      headers: Object.assign({ 'Content-Type': 'application/json', 'X-Admission-Token': u.admission, 'Idempotency-Key': u.key }, u.headers),
      tags: { name: 'reserve' },
    })
    reserveLatency.add(res.timings.duration)
    if (res.status === 201) {
      outcomes.held.add(1)
      u.reservation = res.json('id')
      // 70% pay after a few seconds; 30% wander off and never come back.
      if (Math.random() < 0.7) {
        u.state = 'PAYING'
        u.dueAt = Date.now() + 1000 + Math.random() * Math.min(6000, (HOLD_SECONDS - 3) * 1000)
      } else {
        outcomes.abandoned.add(1)
        u.state = 'DONE'
      }
    } else if (res.status === 503) {
      outcomes.contention.add(1) // nothing happened; same key, try again shortly
      u.section = wanted
      u.dueAt = Date.now() + 300 + Math.random() * 700
    } else if (res.status === 409 && res.json('code') === 'SOLD_OUT' && ++u.tries < 3) {
      outcomes.soldOut.add(1)
      // Look at what is left, like a person would, and try once or twice more.
      const left = http.get(`${BASE}/api/events/${data.id}/availability`, { tags: { name: 'availability' } }).json('sections').filter((x) => x.available > 0)
      if (left.length === 0) {
        outcomes.gaveUp.add(1)
        u.state = 'DONE'
      } else {
        u.section = left[Math.floor(Math.random() * left.length)]
        u.qty = Math.min(u.qty, u.section.available)
        u.key = uuid()
        u.dueAt = Date.now() + 500 + Math.random() * 1500
      }
    } else {
      if (res.status === 409) outcomes.soldOut.add(1)
      else {
        outcomes.other.add(1)
        if (res.status >= 500) outcomes.unexpected5xx.add(1)
      }
      outcomes.gaveUp.add(1)
      u.state = 'DONE'
    }
    return
  }
  if (u.state === 'PAYING') {
    const res = http.post(`${BASE}/api/reservations/${u.reservation}/confirm`, null, { headers: u.headers, tags: { name: 'confirm' } })
    confirmLatency.add(res.timings.duration)
    if (res.status === 200) outcomes.confirmed.add(1)
    else if (res.status >= 500 && res.status !== 503) outcomes.unexpected5xx.add(1)
    if (res.status === 503 && ++u.tries < 6) u.dueAt = Date.now() + 500 // confirm is idempotent: retry
    else u.state = 'DONE'
  }
}

// After the dust settles: every abandoned hold must have expired, and the books must balance.
export function teardown(data) {
  sleep(HOLD_SECONDS + 5)
  const at = new Date().toISOString()
  const position = http.get(`${BASE}/api/admin/events/${data.id}/position?at=${at}`, { headers: ADMIN }).json()
  let analytics
  for (let i = 0; i < 30; i++) { // the Kafka projection trails the sale
    analytics = http.get(`${BASE}/api/admin/events/${data.id}/analytics`, { headers: ADMIN }).json()
    if (analytics.holds === analytics.confirmed + analytics.expired + analytics.released) break
    sleep(1)
  }
  const sum = (f) => position.reduce((n, p) => n + p[f], 0)
  const tag = { tags: { invariant: 'true' } }
  check(null, {
    'no section oversold (sold + held <= total)': () => position.every((p) => p.sold + p.held <= p.total && p.available >= 0),
    'oversoldTickets == 0': () => analytics.oversoldTickets === 0,
    'event stream replay == snapshot': () => analytics.snapshotDrift === 0,
    'no hold outlived its deadline': () => sum('held') === 0,
    'sold <= stock': () => sum('sold') <= STOCK,
    'every hold accounted for downstream (confirmed + expired + released == holds)': () =>
      analytics.holds === analytics.confirmed + analytics.expired + analytics.released,
  }, tag.tags)
  console.log(JSON.stringify({ event: data.id, users: USERS, stock: STOCK, sold: sum('sold'), heldAfterExpiry: sum('held'), analytics, position }))
}

export function handleSummary(data) {
  const m = data.metrics
  const t = (name) => m[name] ? `p50 ${m[name].values.med.toFixed(1)} ms | p95 ${m[name].values['p(95)'].toFixed(1)} ms | p99 ${m[name].values['p(99)'].toFixed(1)} ms | max ${m[name].values.max.toFixed(0)} ms | n ${m[name].values.count}` : 'n/a'
  const c = (name) => (m[name] ? m[name].values.count : 0)
  const failed = Object.entries(m).flatMap(([name, v]) => Object.entries(v.thresholds || {}).filter(([, r]) => !r.ok).map(([th]) => `${name}: ${th}`))
  const text = `
THE LAST TICKET - on-sale load test
users ${USERS} (VUs ${VUS} x ${PER_VU})  stock ${STOCK}  admission ${ADMISSION_RATE}/s  hold ${HOLD_SECONDS}s  availability readers ${READ_RPS} rps

latency
  reserve (critical path)  ${t('reserve_latency')}
  confirm                  ${t('confirm_latency')}
  queue join/poll          ${t('queue_poll_latency')}
  availability (read)      ${t('availability_latency')}
  all http                 ${t('http_req_duration')}

outcomes
  holds placed ${c('reserve_held')}   sold-out refusals ${c('reserve_sold_out')}   503 contention ${c('reserve_contention_503')}   other ${c('reserve_other_error')}
  confirmed ${c('confirm_ok')}   abandoned ${c('holds_abandoned')}   left empty-handed ${c('users_left_empty_handed')}   unexpected 5xx ${c('unexpected_5xx')}
  http requests ${c('http_reqs')}

invariant checks passed: ${m['checks{invariant:true}'] ? m['checks{invariant:true}'].values.passes : '?'} / ${m['checks{invariant:true}'] ? m['checks{invariant:true}'].values.passes + m['checks{invariant:true}'].values.fails : '?'}
thresholds: ${failed.length === 0 ? 'ALL PASSED' : 'FAILED -> ' + failed.join('; ')}
`
  return { stdout: text, [`results/summary-${USERS}.json`]: JSON.stringify(data, null, 2), [`results/summary-${USERS}.txt`]: text }
}

function weighted(sections) {
  let r = Math.random() * SECTIONS.reduce((n, s) => n + s.demand, 0)
  for (const s of SECTIONS) {
    if ((r -= s.demand) < 0) return sections.find((x) => x.ticketType === s.ticketType && x.section === s.section)
  }
  return sections[0]
}

function quantity() {
  const r = Math.random()
  return r < 0.35 ? 1 : r < 0.8 ? 2 : r < 0.88 ? 3 : 4
}

function uuid() {
  return 'xxxxxxxx-xxxx-4xxx-8xxx-xxxxxxxxxxxx'.replace(/x/g, () => Math.floor(Math.random() * 16).toString(16))
}
