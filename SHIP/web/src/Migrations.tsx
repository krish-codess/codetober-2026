import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, ApiError, usePoll, useSamples, type Page, type Plan, type Run, type RunState, type Sample, type Verification } from './api'
import { Chart } from './Chart'
import { ErrorNote, n, Stale, StateBadge } from './ui'

const ACTIVE: RunState[] = ['pending', 'expanding', 'contracting', 'reverting']

export function Migrations() {
  const [selected, setSelected] = useState<number | null>(null)
  const runs = usePoll<Page<Run>>('/v1/migrations?limit=50', 3000)
  const list = runs.data?.items ?? []
  const current = selected ?? list[0]?.id ?? null

  return (
    <div className="split">
      <section aria-labelledby="runs-h">
        <h2 id="runs-h">Runs</h2>
        <Stale of={runs} />
        {runs.loading && <p className="muted">Loading runs…</p>}
        {runs.error && !runs.data && <ErrorNote error={runs.error} retry={runs.refresh} />}
        {runs.data && list.length === 0 && <p className="muted">No migrations yet. Plan one below.</p>}
        <ul className="runs">
          {list.map((r) => (
            <li key={r.id}>
              <button className="run" aria-current={r.id === current} onClick={() => setSelected(r.id)}>
                <span className="run-name">{r.name}</span>
                <StateBadge state={r.state} />
              </button>
            </li>
          ))}
        </ul>
        {runs.data?.next_cursor && <p className="muted">Showing the 50 newest runs.</p>}
        <NewMigration
          onStarted={(id) => {
            setSelected(id)
            runs.refresh()
          }}
        />
      </section>
      <section aria-labelledby="detail-h" aria-live="off">
        {current === null ? (
          <>
            <h2 id="detail-h">Progress</h2>
            <p className="muted">Select a run to see its progress.</p>
          </>
        ) : (
          <RunDetail key={current} id={current} onChanged={runs.refresh} />
        )}
      </section>
    </div>
  )
}

const STEPS = ['Expand', 'Backfill', 'Dual-write', 'Contract'] as const

/** Which step each is at: done, current, failed or pending. Exported for tests. */
export function stepStatus(run: Run): ('done' | 'current' | 'failed' | 'pending')[] {
  const reached = run.finished_at && run.state === 'completed' ? 4 : run.contract_started_at ? 3 : run.expanded_at ? 2 : run.ddl_done_at ? 1 : 0
  const stopped = run.state === 'reverted' || run.state === 'reverting' || run.state === 'failed'
  return STEPS.map((_, i) => (i < reached ? 'done' : i > reached ? 'pending' : stopped ? 'failed' : 'current'))
}

/** "about 3 min left", from the rows done since the backfill began. Exported for tests. */
export function eta(run: Run, now: number): string {
  if (run.state !== 'expanding' || !run.ddl_done_at || run.rows_done <= 0 || run.rows_done >= run.rows_total) return ''
  const elapsed = (now - Date.parse(run.ddl_done_at)) / 1000
  const left = ((run.rows_total - run.rows_done) / run.rows_done) * elapsed
  return left < 90 ? `about ${Math.max(1, Math.round(left))} s left` : `about ${Math.round(left / 60)} min left`
}

function RunDetail({ id, onChanged }: { id: number; onChanged: () => void }) {
  const [fast, setFast] = useState(true)
  const polled = usePoll<Run>(`/v1/migrations/${id}`, fast ? 1000 : 5000)
  const run = polled.data
  const active = !!run && ACTIVE.includes(run.state)
  useEffect(() => setFast(active || !run), [active, run])
  const { samples, error: samplesError } = useSamples(id, active)
  const [busy, setBusy] = useState('')
  const [actionError, setActionError] = useState<ApiError>()
  const [confirm, setConfirm] = useState<'complete' | 'abort' | null>(null)

  if (polled.loading) return <p className="muted">Loading run…</p>
  if (!run) return <ErrorNote error={polled.error!} retry={polled.refresh} />

  const act = async (what: 'verify' | 'complete' | 'abort') => {
    setBusy(what)
    setActionError(undefined)
    try {
      await api(`/v1/migrations/${id}/${what}`, { method: 'POST', body: what === 'abort' ? { reason: 'from the console' } : undefined })
    } catch (e) {
      setActionError(e as ApiError)
    }
    setBusy('')
    polled.refresh() // the run changed (or the attempt was refused): show the truth now, not at the next poll
    onChanged()
  }

  const steps = stepStatus(run)
  const pct = run.rows_total > 0 ? Math.min(100, Math.floor((100 * run.rows_done) / run.rows_total)) : 0
  const t0 = samples.length ? Date.parse(samples[0]!.at) : 0
  const series = (pick: (s: Sample) => number) => samples.map((s) => ({ x: (Date.parse(s.at) - t0) / 1000, y: pick(s) }))
  const secs = (x: number) => `${Math.round(x)} s`
  const canAbort = ['pending', 'expanding', 'expanded'].includes(run.state)

  return (
    <>
      <h2 id="detail-h">
        {run.name} <StateBadge state={run.state} />
      </h2>
      <Stale of={polled} />
      {run.reason && (
        <p role="alert" className={run.state === 'expanded' ? 'note warn' : 'note bad'}>
          <strong>{run.state === 'expanded' ? 'Postponed. ' : 'Stopped. '}</strong>
          {run.reason}
        </p>
      )}

      <ol className="steps" aria-label="Phases">
        {STEPS.map((s, i) => (
          <li key={s} className={steps[i]} aria-current={steps[i] === 'current' ? 'step' : undefined}>
            <span aria-hidden="true">{{ done: '✓', current: '●', failed: '✕', pending: '○' }[steps[i]!]}</span> {s}
            <span className="sr"> ({steps[i]})</span>
          </li>
        ))}
      </ol>

      {run.rows_total > 0 ? (
        <div className="progress">
          <label htmlFor="bf">
            Backfill: {n(run.rows_done)} of about {n(run.rows_total)} rows ({pct}%)
            {eta(run, Date.now()) && <span className="muted"> · {eta(run, Date.now())}</span>}
          </label>
          <progress id="bf" max={run.rows_total} value={Math.min(run.rows_done, run.rows_total)} />
        </div>
      ) : (
        <p className="muted">{run.ddl_done_at ? 'This migration has nothing to backfill.' : 'Waiting for the table lock; rows to backfill are counted once it is granted.'}</p>
      )}

      <p className="muted">
        Compatible application versions: {run.compatible_app_versions.join(', ')}. Aborts if an application session is blocked for more than{' '}
        {n(run.settings.max_lock_wait_ms ?? 0)} ms, if the migration waits more than {run.settings.lock_budget_s} s for locks, or if rolled-back
        transactions exceed the baseline by {run.settings.max_rollbacks_per_s} per second.
      </p>

      <div className="actions">
        <button disabled={run.state !== 'expanded' || !!busy} onClick={() => act('verify')}>
          {busy === 'verify' ? 'Comparing every row…' : 'Verify now'}
        </button>
        <button className="primary" disabled={run.state !== 'expanded' || !!busy} onClick={() => setConfirm('complete')}>
          Complete (drop old column)
        </button>
        <button className="danger" disabled={!canAbort || !!busy} onClick={() => setConfirm('abort')}>
          Abort and revert
        </button>
      </div>
      {actionError && <ErrorNote error={actionError} />}
      <Confirm
        open={confirm}
        onClose={(yes) => {
          const what = confirm
          setConfirm(null)
          if (yes && what) void act(what)
        }}
      />

      <h3>Verification</h3>
      <VerificationTable v={run.verification} state={run.state} />

      <h3>Locks and errors during the migration</h3>
      {samplesError && samples.length > 0 && <p className="note warn">Samples could not be refreshed; showing the {samples.length} already loaded.</p>}
      {samplesError && samples.length === 0 ? (
        <ErrorNote error={samplesError} />
      ) : (
        <>
          <div className="charts">
            <Chart title="Application sessions blocked by the migration" unit="sessions" points={series((s) => s.blocked)} xLabel={secs} />
            <Chart
              title="Longest block of an application session"
              unit="ms"
              points={series((s) => s.max_wait_ms)}
              xLabel={secs}
              limit={{ y: run.settings.max_lock_wait_ms ?? 0, label: 'abort above' }}
            />
            <Chart title="Migration's own lock wait" unit="ms" points={series((s) => s.migrator_wait_ms)} xLabel={secs} />
            <Chart title="Rolled-back transactions above baseline" unit="per s" points={series((s) => s.rollbacks_per_s)} xLabel={secs} />
          </div>
          {samples.length > 0 && (
            <details>
              <summary>Last {Math.min(samples.length, 20)} samples as a table</summary>
              <div className="scroll">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Time</th>
                      <th scope="col">Phase</th>
                      <th scope="col">Blocked</th>
                      <th scope="col">Longest block (ms)</th>
                      <th scope="col">Migration wait (ms)</th>
                      <th scope="col">Rollbacks/s</th>
                      <th scope="col">Rows done</th>
                    </tr>
                  </thead>
                  <tbody>
                    {samples.slice(-20).map((s) => (
                      <tr key={s.at}>
                        <td>{new Date(s.at).toLocaleTimeString()}</td>
                        <td>{s.phase}</td>
                        <td className="num">{s.blocked}</td>
                        <td className="num">{s.max_wait_ms}</td>
                        <td className="num">{s.migrator_wait_ms}</td>
                        <td className="num">{s.rollbacks_per_s.toFixed(1)}</td>
                        <td className="num">{n(s.rows_done)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </>
      )}
    </>
  )
}

export function VerificationTable({ v, state }: { v?: Verification; state: RunState }) {
  if (!v) {
    return <p className="muted">{state === 'expanded' ? 'Comparing every row; this appears when the scan finishes.' : 'Runs when the backfill has finished.'}</p>
  }
  if (v.checks.length === 0) return <p className="muted">This migration has no column with two representations, so there is nothing to compare.</p>
  return (
    <div className="scroll">
      <table>
        <caption>
          <StateBadge state={v.ok ? 'completed' : 'failed'} label={v.ok ? 'Old and new agree' : 'Old and new disagree'} /> · checked{' '}
          {new Date(v.at).toLocaleTimeString()} in {(v.duration_ms / 1000).toFixed(1)} s
        </caption>
        <thead>
          <tr>
            <th scope="col">Column</th>
            <th scope="col">Rows compared</th>
            <th scope="col">Mismatches</th>
            <th scope="col">Not backfilled</th>
            <th scope="col">Unreadable, quarantined</th>
          </tr>
        </thead>
        <tbody>
          {v.checks.map((c) => (
            <tr key={c.table + c.column}>
              <th scope="row">
                {c.table}.{c.column}
              </th>
              <td className="num">{n(c.rows)}</td>
              <td className="num">
                {n(c.mismatches)}
                {c.mismatches > 0 && <span className="muted"> e.g. keys {c.examples.join(', ')}</span>}
              </td>
              <td className="num">{n(c.unbackfilled)}</td>
              <td className="num">{n(c.lossy)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** A native modal dialog: the browser traps focus inside it and returns focus to the button that opened it. */
function Confirm({ open, onClose }: { open: 'complete' | 'abort' | null; onClose: (yes: boolean) => void }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const d = ref.current
    if (open && d && !d.open) d.showModal()
    if (!open && d?.open) d.close()
  }, [open])
  return (
    <dialog ref={ref} onCancel={() => onClose(false)} aria-labelledby="confirm-h">
      <h3 id="confirm-h">{open === 'abort' ? 'Abort and revert this migration?' : 'Drop the old representation?'}</h3>
      <p>
        {open === 'abort'
          ? 'The new columns, triggers and schema version are removed. Applications on the new version fall back to the previous one.'
          : 'This cannot be reverted. It is refused unless verification passes and no connected application depends on the old schema version.'}
      </p>
      <div className="actions">
        <button autoFocus onClick={() => onClose(false)}>
          Keep it as it is
        </button>
        <button className={open === 'abort' ? 'danger' : 'primary'} onClick={() => onClose(true)}>
          {open === 'abort' ? 'Abort and revert' : 'Complete'}
        </button>
      </div>
    </dialog>
  )
}

function NewMigration({ onStarted }: { onStarted: (id: number) => void }) {
  const [text, setText] = useState('')
  const [plan, setPlan] = useState<Plan>()
  const [error, setError] = useState<ApiError>()
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(undefined)
    try {
      let body: unknown
      try {
        body = JSON.parse(text)
      } catch (err) {
        throw new ApiError(0, 'That is not valid JSON: ' + (err as Error).message)
      }
      if (plan) {
        const run = await api<Run>('/v1/migrations', { method: 'POST', body: { name: plan.name, compatible_app_versions: plan.compatible_app_versions, operations: plan.operations } })
        setPlan(undefined)
        setText('')
        onStarted(run.id)
      } else {
        setPlan(await api<Plan>('/v1/plans', { method: 'POST', body }))
      }
    } catch (err) {
      setError(err as ApiError)
    }
    setBusy(false)
  }

  return (
    <form className="new" onSubmit={submit}>
      <h3>Plan a migration</h3>
      <label htmlFor="desired">Desired schema (JSON, as in the desired/ folder)</label>
      <textarea
        id="desired"
        rows={6}
        spellCheck={false}
        value={text}
        onChange={(e) => {
          setText(e.target.value)
          setPlan(undefined)
        }}
        required
      />
      {error && <ErrorNote error={error} />}
      {plan && (
        <div className="scroll">
          <table>
            <caption>
              Plan for {plan.name}
              {plan.estimates.map((e) => (
                <span key={e.table} className="muted">
                  {' '}
                  · {e.table}: about {n(e.rows)} rows{e.backfill_seconds > 0 && `, backfill about ${Math.ceil(e.backfill_seconds)} s`}
                </span>
              ))}
            </caption>
            <thead>
              <tr>
                <th scope="col">Phase</th>
                <th scope="col">Step</th>
                <th scope="col">Lock</th>
                <th scope="col">Blocks</th>
              </tr>
            </thead>
            <tbody>
              {plan.steps.map((s, i) => (
                <tr key={i}>
                  <td>{s.phase}</td>
                  <td>{s.action}</td>
                  <td>{s.lock}</td>
                  <td>{s.blocks}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <button className={plan ? 'primary' : undefined} disabled={busy}>
        {busy ? 'Working…' : plan ? 'Start this migration' : 'Show the plan'}
      </button>
    </form>
  )
}
