import { useEffect, useState } from "react";
import { ApiError, fetchAnalytics, type ShareRateRow, type Superlatives } from "./api";

const KEY = "wrapped:admin-token";
type State =
  | { status: "signed-out" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; superlatives: Superlatives; shareRate: ShareRateRow[] };

const percent = (fraction: number) => `${(fraction * 100).toFixed(1)}%`;

/** Operator view of the two analytical questions: which cards did people get, and which do they share. */
export function Admin() {
  const [token, setToken] = useState(() => sessionStorage.getItem(KEY) ?? "");
  const [draft, setDraft] = useState("");
  const [state, setState] = useState<State>({ status: token ? "loading" : "signed-out" });

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setState({ status: "loading" });
    fetchAnalytics(token)
      .then((data) => !cancelled && setState({ status: "ready", ...data }))
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          sessionStorage.removeItem(KEY);
          setToken("");
          setDraft("");
          setState({ status: "error", message: "That admin token was not accepted." });
        } else {
          setState({ status: "error", message: err instanceof ApiError ? err.message : "Could not load analytics." });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <main className="admin">
      <h1>Wrapped analytics</h1>
      {state.status === "error" && <p role="alert">{state.message}</p>}
      {!token && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            sessionStorage.setItem(KEY, draft);
            setToken(draft);
          }}
        >
          <label htmlFor="admin-token">Admin token</label>
          <input id="admin-token" type="password" value={draft} onChange={(e) => setDraft(e.target.value)} autoComplete="off" required />
          <button type="submit">Sign in</button>
        </form>
      )}
      {state.status === "loading" && <p role="status">Loading analytics…</p>}
      {state.status === "ready" && (
        <>
          <section aria-labelledby="dist">
            <h2 id="dist">Superlative distribution</h2>
            <p className="fine">
              Share of {state.superlatives.users.toLocaleString()} users whose story contains each card. Run {state.superlatives.run_id.slice(0, 8)}.
            </p>
            {state.superlatives.cards.length === 0 ? (
              <p>No payloads in the active run.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th scope="col">Card</th>
                    <th scope="col">Family</th>
                    <th scope="col" className="num">Users</th>
                    <th scope="col">Share of users</th>
                  </tr>
                </thead>
                <tbody>
                  {state.superlatives.cards.map((row) => (
                    <tr key={row.card_type}>
                      <th scope="row">{row.card_type}</th>
                      <td>{row.family}</td>
                      <td className="num">{row.users.toLocaleString()}</td>
                      <td>
                        {/* The number carries the meaning; the bar is a visual aid. */}
                        <span className="bar" style={{ width: `${Math.max(1, row.share_of_users * 100)}%` }} aria-hidden="true" />
                        {percent(row.share_of_users)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
          <section aria-labelledby="rate">
            <h2 id="rate">Share rate by card type</h2>
            <p className="fine">Of the users who saw a card, how many shared it.</p>
            <table>
              <thead>
                <tr>
                  <th scope="col">Card</th>
                  <th scope="col" className="num">Viewers</th>
                  <th scope="col" className="num">Sharers</th>
                  <th scope="col" className="num">Share rate</th>
                </tr>
              </thead>
              <tbody>
                {state.shareRate.map((row) => (
                  <tr key={row.card_type}>
                    <th scope="row">{row.card_type}</th>
                    <td className="num">{row.viewers.toLocaleString()}</td>
                    <td className="num">{row.sharers.toLocaleString()}</td>
                    <td className="num">{row.share_rate === null ? "no views yet" : percent(row.share_rate)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </main>
  );
}
