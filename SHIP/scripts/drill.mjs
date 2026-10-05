#!/usr/bin/env node
// The primary journey, end to end, against the running compose stack, with assertions:
//
//   docker compose down -v && docker compose up -d --build     # fresh stack at schema version 01, v1 serving traffic
//   node scripts/drill.mjs [--kill-controller] [--out docs/evidence/drill.json]
//
// plan 02 from the schema diff -> expand under v1 load -> v2 joins once its schema version is live ->
// contract is refused while v1 is connected -> v1 stops -> contract -> 03 planned, applied, v2 hops forward.
// With --kill-controller the controller is killed mid-backfill first, to show the recovery path.
// Exits non-zero if any writer saw an error or a wrong value, or any step did not go as described.
// Needs Node 20+ and docker compose; no packages.
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'

const root = new URL('..', import.meta.url)
const env = { ...Object.fromEntries(readEnv('.env.example')), ...Object.fromEntries(readEnv('.env')), ...process.env }
const base = env.SHIPD_URL ?? `http://127.0.0.1:${env.SHIPD_PORT ?? 8088}`
const flag = (name) => process.argv.includes(name)
const out = process.argv.includes('--out') ? process.argv[process.argv.indexOf('--out') + 1] : null
const evidence = { started_at: new Date().toISOString(), steps: [] }

function readEnv(file) {
  const path = new URL(file, root)
  if (!existsSync(path)) return []
  return readFileSync(path, 'utf8').split(/\r?\n/).filter((l) => /^[A-Z_]+=/.test(l)).map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)])
}
const json = (file) => JSON.parse(readFileSync(new URL(file, root), 'utf8'))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const compose = (...args) => execFileSync('docker', ['compose', ...args], { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })

function step(msg, data) {
  console.log(`\n== ${msg}`)
  if (data !== undefined) console.log(typeof data === 'string' ? data : JSON.stringify(data))
  evidence.steps.push({ at: new Date().toISOString(), step: msg, data })
}
function check(ok, msg) {
  if (!ok) {
    console.error(`\nFAILED: ${msg}`)
    save()
    process.exit(1)
  }
}
function save() {
  if (out) writeFileSync(new URL(out, root), JSON.stringify(evidence, null, 2) + '\n')
}

async function api(method, path, body, token = env.SHIPD_OPERATOR_TOKEN) {
  const res = await fetch(base + path, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(body && { 'Content-Type': 'application/json' }) },
    body: body && JSON.stringify(body),
  })
  return { status: res.status, body: await res.json().catch(() => ({})) }
}

async function waitFor(what, fn, timeoutS = 3600) {
  const deadline = Date.now() + timeoutS * 1000
  for (;;) {
    const v = await fn().catch(() => null)
    if (v) return v
    check(Date.now() < deadline, `timed out waiting for ${what}`)
    await sleep(1000)
  }
}

async function waitRun(id, state) {
  let last = 0
  return waitFor(`run ${id} to be ${state}`, async () => {
    const { body: run } = await api('GET', `/v1/migrations/${id}`)
    check(!['reverted', 'failed'].includes(run.state) || state === run.state, `run ${id} is ${run.state}: ${run.reason}`)
    if (run.state === 'expanding' && run.rows_total && Date.now() - last > 10000) {
      last = Date.now()
      console.log(`   backfill ${run.rows_done.toLocaleString()} / ~${run.rows_total.toLocaleString()} rows`)
    }
    return run.state === state && (state !== 'expanded' || run.verification) ? run : null
  })
}

const sessions = async (app, schema) => {
  const { body } = await api('GET', '/v1/compat', null, env.SHIPD_VIEWER_TOKEN)
  return body.cells?.find((c) => c.app_version === app && c.schema_version === schema)?.sessions ?? 0
}

// Stops an application version and returns the report it prints on SIGTERM.
function stopShop(service) {
  compose('stop', service)
  const line = compose('logs', '--no-log-prefix', service).split('\n').filter((l) => l.includes('"inserts_acknowledged"')).pop()
  check(line, `${service} printed no report`)
  const report = JSON.parse(line)
  step(`${service} stopped; what it saw`, report)
  check(report.errors === 0 && report.mismatches === 0, `${service} saw ${report.errors} errors and ${report.mismatches} wrong values: ${report.first_errors}`)
  check(report.inserts_acknowledged > 0 && report.rows_checked > 0, `${service} did no work`)
  return report
}

async function submit(name) {
  const desired = json(`desired/${name}.json`)
  const plan = await api('POST', '/v1/plans', desired)
  check(plan.status === 200, `plan ${name}: ${JSON.stringify(plan.body)}`)
  const committed = json(`migrations/${name}.json`)
  check(JSON.stringify(plan.body.operations) === JSON.stringify(committed.operations), `migrations/${name}.json is not what the planner produces from desired/${name}.json`)
  step(`planned ${name} from the schema diff; it matches migrations/${name}.json`, plan.body.steps.map((s) => `${s.phase.padEnd(8)} ${s.lock.padEnd(22)} ${s.action}`).join('\n'))
  const run = await api('POST', '/v1/migrations', committed)
  check(run.status === 202, `submit ${name}: ${run.status} ${JSON.stringify(run.body)}`)
  const again = await api('POST', '/v1/migrations', committed)
  check(again.status === 200 && again.body.id === run.body.id, 'resubmitting did not return the same run')
  return run.body.id
}

// ---- the journey ----

await waitFor('the controller', async () => (await fetch(base + '/readyz')).ok, 300)
await waitFor('v1 traffic', async () => (await sessions('v1', '01_initial')) > 0, 120)
step('v1 is serving traffic on schema version 01_initial', { v1_sessions: await sessions('v1', '01_initial') })

let id = await submit('02_amount_to_cents')
compose('--profile', 'v2', 'up', '-d', '--no-deps', 'shop-v2')
const gate = await api('GET', '/v1/compat/check?app_version=v2', null, env.SHIPD_VIEWER_TOKEN)
step('v2 deployed early; the deploy gate holds it back until its schema version is live', gate.body)
check(gate.body.allowed === false, 'v2 was allowed before the backfill finished')

if (flag('--kill-controller')) {
  await waitFor('the backfill to be under way', async () => {
    const { body: run } = await api('GET', `/v1/migrations/${id}`)
    return run.rows_total > 0 && run.rows_done > run.rows_total * 0.2
  })
  execFileSync('docker', ['kill', compose('ps', '-q', 'shipd').trim()])
  step('controller killed mid-backfill (docker kill, no cleanup)')
  await sleep(5000)
  compose('up', '-d', '--no-deps', 'shipd')
  const run = await waitRun(id, 'reverted')
  step('replacement controller rolled the half-done migration back', { state: run.state, reason: run.reason })
  check(run.reason.includes('controller restarted'), 'unexpected reason')
  id = await submit('02_amount_to_cents')
}

let run = await waitRun(id, 'expanded')
step('expanded: old and new representations coexist and agree', run.verification)
check(run.verification.ok && run.verification.checks[0].mismatches === 0 && run.verification.checks[0].unbackfilled === 0, 'verification failed')

await waitFor('v2 to connect to its schema version', async () => (await sessions('v2', '02_amount_to_cents')) > 0, 120)
await sleep(20000) // both application versions live, reading each other's writes
step('both application versions are live', { v1_on_01: await sessions('v1', '01_initial'), v2_on_02: await sessions('v2', '02_amount_to_cents') })
check((await sessions('v1', '01_initial')) > 0 && (await sessions('v2', '02_amount_to_cents')) > 0, 'both versions should be connected')

const refused = await api('POST', `/v1/migrations/${id}/complete`)
step('contract refused while v1 is connected', refused.body)
check(refused.status === 409 && JSON.stringify(refused.body.errors).includes('shop v1'), 'the gate did not refuse')

const v1 = stopShop('shop-v1')
const verified = await api('POST', `/v1/migrations/${id}/verify`)
step('verified again, after the last v1 write', verified.body)
check(verified.body.ok, 'verification failed')

check((await api('POST', `/v1/migrations/${id}/complete`)).status === 202, 'complete was not accepted')
run = await waitRun(id, 'completed')
step('contracted: the old column is gone', { finished_at: run.finished_at })

const idx = await submit('03_orders_pending_idx')
await waitRun(idx, 'expanded')
check((await api('POST', `/v1/migrations/${idx}/complete`)).status === 202, 'complete of 03 was not accepted')
await waitRun(idx, 'completed')
await waitFor('v2 to follow the schema forward', async () => (await sessions('v2', '03_orders_pending_idx')) > 0, 60)
step('03 applied; v2 moved to the new schema version without a restart', { v2_on_03: await sessions('v2', '03_orders_pending_idx') })

await sleep(5000)
const v2 = stopShop('shop-v2')
compose('--profile', 'v2', 'up', '-d', '--no-deps', 'shop-v2') // leave the stack serving

const duration = (await api('GET', '/v1/analytics/duration-by-size', null, env.SHIPD_VIEWER_TOKEN)).body
const locks = (await api('GET', '/v1/analytics/lock-waits', null, env.SHIPD_VIEWER_TOKEN)).body
step('migration duration by table size', duration)
step('lock wait time during migration', locks)
evidence.summary = {
  writer_errors: v1.errors + v2.errors,
  wrong_values_read: v1.mismatches + v2.mismatches,
  writes_acknowledged: v1.inserts_acknowledged + v2.inserts_acknowledged,
  rows_cross_checked: v1.rows_checked + v2.rows_checked,
  v1_latency_ms: { p50: v1.p50_ms, p99: v1.p99_ms, max: v1.max_ms },
  v2_latency_ms: { p50: v2.p50_ms, p99: v2.p99_ms, max: v2.max_ms },
  longest_application_block_ms: Math.max(...locks.items.map((w) => w.max_wait_ms)),
}
step('SUMMARY', evidence.summary)
save()
console.log('\nOK: schema changed twice under live load; no writer saw an error or a wrong value.')
