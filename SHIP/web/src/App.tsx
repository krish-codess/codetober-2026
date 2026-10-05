import { useEffect, useState, type FormEvent } from 'react'
import { Analytics } from './Analytics'
import { api, ApiError, clearCache, token } from './api'
import { Compat } from './Compat'
import { Migrations } from './Migrations'

const VIEWS = [
  { id: 'migrations', label: 'Migrations', view: Migrations },
  { id: 'compatibility', label: 'Compatibility', view: Compat },
  { id: 'analytics', label: 'Analytics', view: Analytics },
] as const

const fromHash = () => VIEWS.find((v) => '#' + v.id === location.hash)?.id ?? 'migrations'

export function App() {
  const [signedIn, setSignedIn] = useState(() => token.get() !== '')
  const [view, setView] = useState(fromHash)
  useEffect(() => {
    const on = () => setView(fromHash())
    addEventListener('hashchange', on)
    return () => removeEventListener('hashchange', on)
  }, [])

  if (!signedIn) return <SignIn onDone={() => setSignedIn(true)} />
  const View = VIEWS.find((v) => v.id === view)!.view
  return (
    <>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header>
        <h1>shipd</h1>
        <nav aria-label="Views">
          {VIEWS.map((v) => (
            <a key={v.id} href={'#' + v.id} aria-current={v.id === view ? 'page' : undefined}>
              {v.label}
            </a>
          ))}
        </nav>
        <button
          onClick={() => {
            token.clear()
            clearCache()
            setSignedIn(false)
          }}
        >
          Sign out
        </button>
      </header>
      <main id="main" tabIndex={-1}>
        <View />
      </main>
    </>
  )
}

function SignIn({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    token.set(value.trim())
    try {
      await api('/v1/compat') // the cheapest authenticated call: proves the token before showing anything
      onDone()
    } catch (err) {
      token.clear()
      setError((err as ApiError).status === 401 ? 'That token was not accepted.' : (err as ApiError).message)
    }
    setBusy(false)
  }
  return (
    <main className="signin">
      <h1>shipd</h1>
      <form onSubmit={submit}>
        <label htmlFor="token">API token</label>
        <input id="token" type="password" autoComplete="off" autoFocus required value={value} onChange={(e) => setValue(e.target.value)} aria-describedby="token-help" />
        <p id="token-help" className="muted">
          The viewer token can watch. The operator token can also start, complete and abort migrations.
        </p>
        {error && (
          <p role="alert" className="note bad">
            {error}
          </p>
        )}
        <button className="primary" disabled={busy}>
          {busy ? 'Checking…' : 'Sign in'}
        </button>
      </form>
    </main>
  )
}
