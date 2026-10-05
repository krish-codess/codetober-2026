import { useState, type FormEvent } from 'react'
import { api, ApiError, usePoll, type Matrix } from './api'
import { ErrorNote, Stale } from './ui'

interface Check {
  app_version: string
  allowed: boolean
  schema_version?: string
  reason: string
}

export function Compat() {
  const m = usePoll<Matrix>('/v1/compat', 2000)
  const [version, setVersion] = useState('')
  const [check, setCheck] = useState<Check>()
  const [error, setError] = useState<ApiError>()

  const ask = async (e: FormEvent) => {
    e.preventDefault()
    setError(undefined)
    setCheck(undefined)
    try {
      setCheck(await api<Check>('/v1/compat/check?app_version=' + encodeURIComponent(version)))
    } catch (err) {
      setError(err as ApiError)
    }
  }

  return (
    <section aria-labelledby="compat-h">
      <h2 id="compat-h">Compatibility matrix</h2>
      <p className="muted">
        Which application versions can run on which schema versions, and how many connections each has open right now. A schema version is only dropped
        when no connection still depends on it.
      </p>
      <Stale of={m} />
      {m.loading && <p className="muted">Loading the matrix…</p>}
      {m.error && !m.data && <ErrorNote error={m.error} retry={m.refresh} />}
      {m.data && m.data.schema_versions.length === 0 && <p className="muted">No schema version exists yet. The first migration creates one.</p>}
      {m.data && m.data.schema_versions.length > 0 && (
        <div className="scroll">
          <table className="matrix">
            <thead>
              <tr>
                <th scope="col">Application version</th>
                {m.data.schema_versions.map((v) => (
                  <th scope="col" key={v.version}>
                    {v.version}
                    <br />
                    <span className={`badge ${v.live ? 'good' : 'idle'}`}>
                      <span aria-hidden="true">{v.live ? '●' : '○'}</span> {v.live ? 'live' : 'retired'}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {m.data.app_versions.length === 0 && (
                <tr>
                  <td colSpan={m.data.schema_versions.length + 1} className="muted">
                    No application version is declared or connected.
                  </td>
                </tr>
              )}
              {m.data.app_versions.map((a) => (
                <tr key={a}>
                  <th scope="row">{a}</th>
                  {m.data!.schema_versions.map((v) => {
                    const c = m.data!.cells.find((x) => x.app_version === a && x.schema_version === v.version)
                    const trouble = !!c && c.sessions > 0 && !c.compatible
                    return (
                      <td key={v.version} className={c?.compatible ? 'yes' : 'no'}>
                        <span aria-hidden="true">{c?.compatible ? '✓' : '—'}</span> {c?.compatible ? 'compatible' : 'not compatible'}
                        <br />
                        <span className={trouble ? 'badge bad' : 'muted'}>
                          {c?.sessions ?? 0} {c?.sessions === 1 ? 'connection' : 'connections'}
                          {trouble && ' (should not be here)'}
                        </span>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <form className="inline" onSubmit={ask}>
        <h3>Deploy gate</h3>
        <label htmlFor="appv">May this application version be rolled out now?</label>
        <div className="row">
          <input id="appv" value={version} onChange={(e) => setVersion(e.target.value)} placeholder="v2" required pattern="[A-Za-z0-9._\-]{1,20}" />
          <button>Check</button>
        </div>
        {error && <ErrorNote error={error} />}
        {check && (
          <p role="status" className={`note ${check.allowed ? 'good' : 'warn'}`}>
            <strong>{check.allowed ? '✓ Yes. ' : '✕ Not yet. '}</strong>
            {check.app_version}: {check.reason}.
          </p>
        )}
      </form>
    </section>
  )
}
