// Synthetic on-sale purchase-attempt feed, deterministic for a given --seed.
// There is no public feed of real on-sale traffic, so this reproduces its *shape* (lobby trickle, a wall of requests at
// T0, exponential decay, long tail) and its *defects* (see DEFECTS). The backend ingests this file through the same
// reservation path live HTTP traffic uses (POST /api/admin/attempts/ingest).
//
//   node loadtest/generate-attempts.mjs --users 10000 --seed 42 --out data/generated/attempts.ndjson
import { writeFileSync, mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

const args = Object.fromEntries(process.argv.slice(2).join(' ').split('--').filter(Boolean).map((a) => a.trim().split(/\s+/)))
const USERS = Number(args.users ?? 10000)
const SEED = Number(args.seed ?? 42)
const OUT = args.out ?? 'data/generated/attempts.ndjson'
const EVENT_ID = args.event ?? '00000000-0000-4000-8000-000000000001'
const T0 = Date.parse(args.t0 ?? '2026-10-10T10:00:00Z')

// mulberry32: tiny seeded PRNG, good enough for shaping test data
let s = SEED >>> 0
const rnd = () => {
  s = (s + 0x6d2b79f5) >>> 0
  let t = s
  t = Math.imul(t ^ (t >>> 15), t | 1)
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296
}
const pick = (weighted) => {
  let r = rnd() * weighted.reduce((a, [, w]) => a + w, 0)
  for (const [v, w] of weighted) if ((r -= w) < 0) return v
  return weighted[0][0]
}
const uuid = () => 'xxxxxxxx-xxxx-4xxx-8xxx-xxxxxxxxxxxx'.replace(/x/g, () => Math.floor(rnd() * 16).toString(16))

// Demand is deliberately skewed towards the scarce inventory: that is what makes on-sales hard.
const INVENTORY = [[['GA', 'FLOOR'], 40], [['GA', 'LOWER'], 20], [['GA', 'UPPER'], 12], [['VIP', 'FLOOR'], 18], [['VIP', 'BOX'], 10]]
const QTY = [[1, 35], [2, 45], [3, 8], [4, 12]]

// Arrival offset from T0 in ms: 15% already waiting, 70% in an exponential burst (mean 4s), 15% long tail to 5 minutes.
const arrival = () => {
  const r = rnd()
  if (r < 0.15) return -Math.floor(rnd() * 120_000)
  if (r < 0.85) return Math.floor(-Math.log(1 - rnd()) * 4000)
  return Math.floor(rnd() * 300_000)
}

const DEFECTS = {
  qtyAsString: 0.03, // "2" instead of 2 (form-encoded clients)
  dirtySection: 0.04, // " floor ", "Floor"
  epochTs: 0.03, // client_ts as epoch millis
  localTs: 0.02, // client_ts as "10/10/2026 10:00:03" (no zone)
  missingField: 0.01, // a required field absent or null
  badQty: 0.01, // 0, -1, 99, 2.5
  unknownSection: 0.005,
  truncated: 0.003, // line cut mid-JSON
  duplicate: 0.06, // client retry: same attempt_id sent again
  late: 0.03, // delivered far out of order
}
const counts = Object.fromEntries(Object.keys(DEFECTS).map((k) => [k, 0]))
const hit = (k) => rnd() < DEFECTS[k] && ++counts[k]

const rows = []
for (let i = 0; i < USERS; i++) {
  const [ticketType, section] = pick(INVENTORY)
  const at = T0 + arrival()
  const rec = {
    attempt_id: uuid(),
    user_id: `u-${String(i).padStart(6, '0')}`,
    event_id: EVENT_ID,
    ticket_type: ticketType,
    section,
    quantity: pick(QTY),
    client_ts: new Date(at).toISOString(),
  }
  if (hit('qtyAsString')) rec.quantity = String(rec.quantity)
  if (hit('dirtySection')) rec.section = rnd() < 0.5 ? ` ${section.toLowerCase()} ` : section[0] + section.slice(1).toLowerCase()
  if (hit('epochTs')) rec.client_ts = at
  else if (hit('localTs')) {
    const d = new Date(at)
    const p = (n) => String(n).padStart(2, '0')
    rec.client_ts = `${p(d.getUTCDate())}/${p(d.getUTCMonth() + 1)}/${d.getUTCFullYear()} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`
  }
  if (hit('missingField')) {
    const f = pick([['user_id', 1], ['quantity', 1], ['section', 1], ['attempt_id', 1]])
    if (rnd() < 0.5) delete rec[f]
    else rec[f] = null
  }
  if (hit('badQty')) rec.quantity = pick([[0, 1], [-1, 1], [99, 1], [2.5, 1]])
  if (hit('unknownSection')) rec.section = 'BALCONY'
  let line = JSON.stringify(rec)
  if (hit('truncated')) line = line.slice(0, Math.floor(line.length * (0.3 + rnd() * 0.5)))
  rows.push({ at, line })
  if (hit('duplicate')) rows.push({ at: at + 200 + Math.floor(rnd() * 3000), line })
}
// Delivery order = arrival order, except late records which surface 10-60s after they were sent.
for (const r of rows) r.delivered = hit('late') ? r.at + 10_000 + Math.floor(rnd() * 50_000) : r.at
rows.sort((a, b) => a.delivered - b.delivered)

mkdirSync(dirname(OUT), { recursive: true })
writeFileSync(OUT, rows.map((r) => r.line).join('\n') + '\n')
console.error(JSON.stringify({ out: OUT, seed: SEED, users: USERS, lines: rows.length, injected: counts }))
