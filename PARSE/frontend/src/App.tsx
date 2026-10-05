import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import { Label } from "./Label";
import { Performance } from "./Performance";
import { TaxonomyAdmin } from "./TaxonomyAdmin";
import { ApiError, type Principal, type Taxonomy, api, getToken, setToken } from "./api";

type Tab = "label" | "performance" | "taxonomy";
const TABS: { id: Tab; title: string; minRole: number }[] = [
  { id: "label", title: "Label", minRole: 1 },
  { id: "performance", title: "Performance", minRole: 0 },
  { id: "taxonomy", title: "Taxonomy", minRole: 0 },
];
const RANK = { viewer: 0, annotator: 1, admin: 2 } as const;

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const e = error instanceof ApiError ? error : new ApiError(0, "unknown", String(error));
  return (
    <div className="notice error" role="alert">
      <strong>Something went wrong.</strong> {e.message}
      {e.requestId && <span className="muted"> (request {e.requestId.slice(0, 8)})</span>}
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function Loading({ what, children }: { what: string; children?: ReactNode }) {
  return (
    <output className="skeleton" aria-busy="true">
      Loading {what}…{children}
    </output>
  );
}

// Shared by every view; long-lived because the taxonomy only changes through an explicit admin
// action, which invalidates this key.
export const useTaxonomy = () =>
  useQuery({ queryKey: ["taxonomy"], queryFn: () => api<Taxonomy>("/taxonomy"), staleTime: 5 * 60_000 });

function Login({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setToken(value.trim());
    try {
      await api<Principal>("/me");
      onDone();
    } catch (e) {
      setToken(null);
      setError(e instanceof ApiError && e.status === 401 ? "That token was not recognised." : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login">
      <h1>What are they actually complaining about</h1>
      <form onSubmit={submit}>
        <label htmlFor="token">Access token</label>
        <input
          id="token"
          type="password"
          autoComplete="off"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-describedby={error ? "token-error" : undefined}
          aria-invalid={error ? true : undefined}
          required
        />
        {error && (
          <p id="token-error" className="field-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "Checking…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

export function App() {
  const qc = useQueryClient();
  const [authed, setAuthed] = useState(() => getToken() !== null);
  const [tab, setTab] = useState<Tab>(() => (window.location.hash.slice(1) as Tab) || "label");
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => api<Principal>("/me"),
    enabled: authed,
    staleTime: Number.POSITIVE_INFINITY,
  });

  useEffect(() => {
    window.location.hash = tab;
  }, [tab]);
  useEffect(() => {
    if (me.error instanceof ApiError && me.error.status === 401) signOut();
  });

  function signOut() {
    setToken(null);
    qc.clear();
    setAuthed(false);
  }

  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  if (me.isPending) return <Loading what="your session" />;
  if (me.isError) return <ErrorBox error={me.error} onRetry={() => me.refetch()} />;

  const rank = RANK[me.data.role];
  const tabs = TABS.filter((t) => rank >= t.minRole);
  const active = tabs.some((t) => t.id === tab) ? tab : (tabs[0]?.id ?? "performance");
  return (
    <>
      <header className="top">
        <span className="brand">Complaints</span>
        <nav aria-label="Sections">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              className="tab"
              aria-current={active === t.id ? "page" : undefined}
              onClick={() => setTab(t.id)}
            >
              {t.title}
            </button>
          ))}
        </nav>
        <span className="who">
          {me.data.name} · {me.data.role}
          <button type="button" className="link" onClick={signOut}>
            Sign out
          </button>
        </span>
      </header>
      <main id="main">
        {active === "label" && <Label />}
        {active === "performance" && <Performance />}
        {active === "taxonomy" && <TaxonomyAdmin role={me.data.role} />}
      </main>
    </>
  );
}
