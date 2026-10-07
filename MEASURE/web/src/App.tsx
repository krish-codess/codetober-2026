import { useEffect, useState } from "react";
import { Admin } from "./Admin";
import { Story, usableCards } from "./Story";
import { useWrapped, type LoadStep } from "./useWrapped";

const TOKEN_KEY = "wrapped:token";

/** The personal link carries the token in the fragment (never sent to a server, never in a Referer). */
function takeToken(): string | null {
  const fromLink = new URLSearchParams(window.location.hash.slice(1)).get("t");
  if (fromLink) {
    sessionStorage.setItem(TOKEN_KEY, fromLink);
    // Drop it from the address bar so it does not end up in a screenshot or a copied URL.
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    return fromLink;
  }
  return sessionStorage.getItem(TOKEN_KEY);
}

const STEPS: Record<LoadStep, { n: number; label: string }> = {
  connecting: { n: 1, label: "Finding your year" },
  receiving: { n: 2, label: "Receiving your story" },
  preparing: { n: 3, label: "Setting the stage" },
};

function Notice({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <main className="notice">
      <h1>{title}</h1>
      {children}
    </main>
  );
}

function Wrapped({ token, onForget }: { token: string; onForget: () => void }) {
  const { state, reload } = useWrapped(token);
  if (state.status === "loading") {
    const step = STEPS[state.step];
    return (
      <Notice title="Your year is on its way">
        <progress max={3} value={step.n} aria-label="Loading your story" />
        <p role="status">
          {step.label} ({step.n} of 3){state.attempt > 1 && `. Attempt ${state.attempt} of ${state.attempts}.`}
        </p>
      </Notice>
    );
  }
  if (state.status === "unauthorized") {
    return (
      <Notice title="This link no longer works">
        <p>Personal links expire. Open the most recent one you were sent.</p>
        <button type="button" onClick={onForget}>
          Use a different link
        </button>
      </Notice>
    );
  }
  if (state.status === "empty") {
    return (
      <Notice title="No story this year">
        <p>We did not find any activity for this account in the year, so there is nothing to wrap. See you next year.</p>
      </Notice>
    );
  }
  if (state.status === "error") {
    return (
      <Notice title="We could not load your year">
        <p role="alert">{state.message}</p>
        {state.requestId && <p className="fine">If you contact support, quote request {state.requestId}.</p>}
        <button type="button" onClick={reload}>
          Try again
        </button>
      </Notice>
    );
  }
  if (usableCards(state.wrapped.cards).length === 0) {
    return (
      <Notice title="Your story is not ready">
        <p>We found your account, but there are no cards we can show yet.</p>
        <button type="button" onClick={reload}>
          Check again
        </button>
      </Notice>
    );
  }
  return <Story token={token} wrapped={state.wrapped} stale={state.stale} savedAt={state.savedAt} onReload={reload} />;
}

export function App() {
  const [token, setToken] = useState<string | null>(takeToken);
  const [route, setRoute] = useState(window.location.hash);
  const [pasted, setPasted] = useState("");

  useEffect(() => {
    const onHash = () => {
      setRoute(window.location.hash);
      const next = takeToken();
      if (next) setToken(next);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (route === "#admin") return <Admin />;
  if (token) {
    return (
      <Wrapped
        token={token}
        onForget={() => {
          sessionStorage.removeItem(TOKEN_KEY);
          setToken(null);
        }}
      />
    );
  }
  return (
    <Notice title="Your App, Wrapped">
      <p>Open the personal link you were sent to see your year. If you have the link's code, paste it here.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const value = pasted.trim().split("#t=").pop() ?? "";
          if (!value) return;
          sessionStorage.setItem(TOKEN_KEY, value);
          setToken(value);
        }}
      >
        <label htmlFor="token">Personal link or code</label>
        <input id="token" value={pasted} onChange={(e) => setPasted(e.target.value)} autoComplete="off" spellCheck={false} required />
        <button type="submit">Show my year</button>
      </form>
    </Notice>
  );
}
