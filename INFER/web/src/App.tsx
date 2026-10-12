import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  ApiError,
  createPolicy,
  downloadPolicy,
  getJson,
  getToken,
  setToken,
  type Dataset,
  type Decile,
  type Experiment,
  type Group,
  type Page,
  type Policy,
  type Profiles,
  type Run,
  type Segment,
  type Verdict,
} from "./api";
import { DecileChart, QiniChart, label, points } from "./Charts";
import { useResource, type Resource } from "./useResource";

const VERDICT: Record<Verdict, string> = { helps: "▲ helps", hurts: "▼ hurts", unclear: "◆ unclear" };
const TABS = [
  ["compare", "Compare models"],
  ["segments", "Who it helps and hurts"],
  ["policy", "Targeting policy"],
] as const;
type Tab = (typeof TABS)[number][0];
const time = (at: number) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const pct = (v: number, digits = 0) => `${(v * 100).toFixed(digits)}%`;
const money = (v: number) => v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** One place decides what loading, empty, error, and stale look like, so no view can forget one. */
function Gate<T>({ r, what, reload, onUnauthorized, children }: { r: Resource<T>; what: string; reload: () => void; onUnauthorized: () => void; children: (data: T) => ReactNode }) {
  useEffect(() => {
    if (r.status === "unauthorized") onUnauthorized();
  }, [r.status, onUnauthorized]);
  if (r.status === "loading")
    return (
      <p role="status" className="state">
        <progress max={r.attempts} value={r.attempt - 0.5} aria-label={`Loading ${what}`} /> Loading {what}
        {r.attempt > 1 ? `: attempt ${r.attempt} of ${r.attempts} after a failed try` : "…"}
      </p>
    );
  if (r.status === "unauthorized") return null;
  if (r.status === "empty")
    return (
      <p className="state" role="status">
        <strong>Nothing here yet.</strong> {r.message}
      </p>
    );
  if (r.status === "error")
    return (
      <div className="state error" role="alert">
        <p>
          <strong>Could not load {what}.</strong> {r.message}
          {r.requestId && <small> Reference: {r.requestId}</small>}
        </p>
        <button onClick={reload}>Try again</button>
      </div>
    );
  return (
    <>
      {r.stale && (
        <div className="banner" role="status">
          ⚠ <strong>Showing saved results from {time(r.at)}.</strong> The server could not be reached to refresh {what}. <button onClick={reload}>Retry now</button>
        </div>
      )}
      {children(r.data)}
    </>
  );
}

export function App() {
  const [token, setTok] = useState(getToken());
  const [rejected, setRejected] = useState(false);
  const signOut = (wasRejected: boolean) => {
    setToken("");
    setTok("");
    setRejected(wasRejected);
  };
  if (!token) {
    return (
      <SignIn
        rejected={rejected}
        onToken={(t) => {
          setToken(t);
          setTok(t);
        }}
      />
    );
  }
  return <Dashboard onUnauthorized={() => signOut(true)} onSignOut={() => signOut(false)} />;
}

function SignIn({ rejected, onToken }: { rejected: boolean; onToken: (t: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <main className="signin">
      <h1>Who actually needed the discount</h1>
      <p>Uplift models compared on Qini, the segments treatment helps and hurts, and a budgeted targeting policy.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) onToken(value.trim());
        }}
      >
        <label htmlFor="token">Access token</label>
        <input id="token" type="password" autoComplete="off" autoFocus value={value} onChange={(e) => setValue(e.target.value)} aria-describedby={rejected ? "token-error" : undefined} />
        {rejected && (
          <p id="token-error" role="alert" className="error">
            That token was not accepted. Check it and try again.
          </p>
        )}
        <button type="submit">Open dashboard</button>
      </form>
    </main>
  );
}

function Dashboard({ onUnauthorized, onSignOut }: { onUnauthorized: () => void; onSignOut: () => void }) {
  const datasets = useResource<Dataset[]>("/v1/datasets");
  const [chosen, setChosen] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("compare");
  const heading = useRef<HTMLHeadingElement>(null);
  const moved = useRef(false);
  useEffect(() => {
    if (moved.current) heading.current?.focus(); // after a tab change, keyboard and screen-reader users land on the new content
  }, [tab]);

  return (
    <>
      <a className="skip" href="#content">
        Skip to content
      </a>
      <header>
        <h1>Who actually needed the discount</h1>
        <button className="quiet" onClick={onSignOut}>
          Sign out
        </button>
      </header>
      <Gate r={datasets.state} what="datasets" reload={datasets.reload} onUnauthorized={onUnauthorized}>
        {(list) => {
          const usable = list.filter((d) => d.experiment_id);
          if (!usable.length)
            return (
              <p className="state" role="status">
                <strong>No trained experiment yet.</strong> Run <code>uplift seed</code> to ingest data and train the models.
              </p>
            );
          const dataset = usable.find((d) => d.name === chosen) ?? usable[0];
          return (
            <>
              <div className="toolbar">
                <label>
                  Dataset{" "}
                  <select value={dataset.name} onChange={(e) => setChosen(e.target.value)}>
                    {usable.map((d) => (
                      <option key={d.name}>{d.name}</option>
                    ))}
                  </select>
                </label>
                <span className="meta">
                  {dataset.units.toLocaleString()} units · outcome: {dataset.outcome} · {dataset.quarantined.toLocaleString()} rows quarantined
                </span>
              </div>
              <nav aria-label="Views">
                {TABS.map(([id, name]) => (
                  <button
                    key={id}
                    aria-current={tab === id ? "page" : undefined}
                    onClick={() => {
                      moved.current = true;
                      setTab(id);
                    }}
                  >
                    {name}
                  </button>
                ))}
              </nav>
              <main id="content">
                <h2 ref={heading} tabIndex={-1}>
                  {TABS.find(([id]) => id === tab)![1]}
                </h2>
                <ExperimentView key={dataset.name} dataset={dataset} tab={tab} onUnauthorized={onUnauthorized} />
              </main>
            </>
          );
        }}
      </Gate>
    </>
  );
}

function ExperimentView({ dataset, tab, onUnauthorized }: { dataset: Dataset; tab: Tab; onUnauthorized: () => void }) {
  const experiment = useResource<Experiment>(`/v1/datasets/${dataset.name}/experiment`);
  return (
    <Gate r={experiment.state} what="the experiment" reload={experiment.reload} onUnauthorized={onUnauthorized}>
      {(e) => {
        const failed = e.runs.filter((r) => r.status === "failed");
        const champion = e.runs.find((r) => r.is_champion);
        return (
          <>
            {e.stale && (
              <div className="banner" role="status">
                ⚠ <strong>These models are out of date.</strong> New data was ingested after they were trained{e.finished_at && ` (${new Date(e.finished_at).toLocaleDateString()})`}. Retrain to include it.
              </div>
            )}
            {failed.length > 0 && (
              <div className="banner" role="status">
                ⚠ <strong>Partial results.</strong> {failed.length} of {e.runs.length} models failed to train and are missing from the comparison:{" "}
                {failed.map((r) => `${label(r.learner)} (${r.error})`).join("; ")}
              </div>
            )}
            {tab === "compare" && <Compare e={e} />}
            {tab === "segments" && champion && <Segments e={e} champion={champion} onUnauthorized={onUnauthorized} />}
            {tab === "policy" && champion && <PolicyBuilder champion={champion} outcome={e.outcome} onUnauthorized={onUnauthorized} />}
          </>
        );
      }}
    </Gate>
  );
}

function Compare({ e }: { e: Experiment }) {
  const [kind, setKind] = useState<"qini" | "uplift">("qini");
  const runs = e.runs.filter((r) => r.metrics).sort((a, b) => b.metrics!.test.qini - a.metrics!.test.qini);
  const champion = runs.find((r) => r.is_champion);
  const hasOracle = runs.some((r) => r.metrics!.test.qini_share_of_oracle !== undefined);
  return (
    <>
      {champion && (
        <p className="lede">
          <strong>{label(champion.learner)}</strong> ranks customers best on held-out data
          {champion.metrics!.test.p_beats_response !== undefined && (
            <>
              ; it beats targeting likely buyers in <strong>{pct(champion.metrics!.test.p_beats_response)}</strong> of bootstrap resamples
            </>
          )}
          .
          {e.audit?.reference && !e.audit.reference.champion_beats_response_on_val && " On the validation split it did not beat the likely-buyers baseline: treat this as a finding, not a model to ship."}
        </p>
      )}
      <fieldset className="toggle">
        <legend>Curve</legend>
        {(["qini", "uplift"] as const).map((k) => (
          <label key={k}>
            <input type="radio" name="kind" checked={kind === k} onChange={() => setKind(k)} /> {k === "qini" ? "Qini" : "Cumulative uplift"}
          </label>
        ))}
      </fieldset>
      <QiniChart runs={e.runs} kind={kind} />
      <div className="scroll">
        <table>
          <caption>Uplift metrics on the held-out test split. Models are never compared on classification accuracy.</caption>
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">Qini ×100</th>
              <th scope="col">95% interval</th>
              {hasOracle && <th scope="col">Share of best possible</th>}
              <th scope="col">Beats likely-buyers</th>
              <th scope="col">Uplift in top 20%</th>
              <th scope="col">Calibration slope</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => {
              const m = r.metrics!.test;
              return (
                <tr key={r.id}>
                  <th scope="row">
                    {label(r.learner)}
                    {r.is_champion && " ★"}
                  </th>
                  <td>{(m.qini * 100).toFixed(2)}</td>
                  <td>
                    {(m.qini_ci95[0] * 100).toFixed(2)} to {(m.qini_ci95[1] * 100).toFixed(2)}
                  </td>
                  {hasOracle && <td>{m.qini_share_of_oracle === undefined ? "—" : pct(m.qini_share_of_oracle)}</td>}
                  <td>{m.p_beats_response === undefined ? "—" : pct(m.p_beats_response)}</td>
                  <td>{points(m.uplift_at["0.2"])}</td>
                  <td>{r.metrics!.calibration_slope === null ? "—" : r.metrics!.calibration_slope.toFixed(2)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {e.audit && (
        <p className="meta">
          Randomization check: {pct(e.audit.treated_share, 1)} treated (ratio test p = {e.audit.srm_p_value.toFixed(2)}); features predict treatment with AUC{" "}
          {e.audit.treatment_auc.toFixed(3)} (0.5 means no leakage). Split: {e.split_strategy}. Code {e.code_version}.
        </p>
      )}
    </>
  );
}

function GroupCard({ title, group }: { title: string; group: Group | null | undefined }) {
  if (!group)
    return (
      <section className="card">
        <h3>{title}</h3>
        <p>The model predicts harm for too few held-out customers to profile.</p>
      </section>
    );
  return (
    <section className="card">
      <h3>{title}</h3>
      <p className="big">
        {points(group.observed_uplift)} <span className={`verdict ${group.verdict}`}>{VERDICT[group.verdict]}</span>
      </p>
      <p className="meta">
        observed on {group.n.toLocaleString()} held-out customers ({pct(group.share_of_population, 1)})
        {group.ci_low !== null && group.ci_high !== null && (
          <>
            , 95% interval {points(group.ci_low)} to {points(group.ci_high)}
          </>
        )}
        {group.mean_predicted !== null && <>; model predicted {points(group.mean_predicted)}</>}
        {group.true_uplift !== null && <>; true effect {points(group.true_uplift)}</>}
      </p>
      <h4>What sets them apart</h4>
      <ul className="traits">
        {group.traits.slice(0, 5).map((t) => (
          <li key={t.feature + t.level}>
            {t.feature} = {t.level}: <strong>{pct(t.share_in_group)}</strong> of the group vs {pct(t.share_overall)} overall
          </li>
        ))}
      </ul>
    </section>
  );
}

function Segments({ e, champion, onUnauthorized }: { e: Experiment; champion: Run; onUnauthorized: () => void }) {
  const deciles = useResource<Decile[]>(`/v1/runs/${champion.id}/deciles`);
  const profiles = useResource<Profiles>(`/v1/runs/${champion.id}/profiles`);
  return (
    <>
      <p className="lede">
        Ranked by {label(champion.learner)}. If the model is right, observed uplift falls from left to right, and the last bars show who the treatment does nothing
        for, or harms.
      </p>
      <Gate r={deciles.state} what="the decile analysis" reload={deciles.reload} onUnauthorized={onUnauthorized}>
        {(d) => <DecileChart deciles={d} />}
      </Gate>
      <Gate r={profiles.state} what="the group profiles" reload={profiles.reload} onUnauthorized={onUnauthorized}>
        {(p) => (
          <div className="cards">
            <GroupCard title="Highest predicted uplift (top 10%)" group={p.high_uplift} />
            <GroupCard title="Predicted to be harmed" group={p.negative_uplift} />
          </div>
        )}
      </Gate>
      <SegmentScan experimentId={e.id} onUnauthorized={onUnauthorized} />
    </>
  );
}

function SegmentScan({ experimentId, onUnauthorized }: { experimentId: string; onUnauthorized: () => void }) {
  const [verdict, setVerdict] = useState<Verdict | "">("hurts");
  const [items, setItems] = useState<Segment[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [message, setMessage] = useState("");
  const load = async (after: string | null, replace: boolean) => {
    setState("loading");
    try {
      const query = new URLSearchParams({ limit: "25", ...(verdict && { verdict }), ...(after && { cursor: after }) });
      const page = await getJson<Page<Segment>>(`/v1/experiments/${experimentId}/segments?${query}`);
      setItems((old) => (replace ? page.items : [...old, ...page.items]));
      setCursor(page.next_cursor);
      setState("ready");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return onUnauthorized();
      setMessage(err instanceof Error ? err.message : "Unknown error");
      setState("error");
    }
  };
  useEffect(() => {
    void load(null, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reload from the first page whenever the filter changes
  }, [verdict, experimentId]);

  return (
    <section>
      <h3>Segment scan</h3>
      <p className="meta">
        Model-free: observed uplift in every one- and two-feature segment of the whole experiment, corrected for testing many segments at once. A segment is only
        called “hurts” when the corrected p-value is under 5%.
      </p>
      <label>
        Show{" "}
        <select value={verdict} onChange={(ev) => setVerdict(ev.target.value as Verdict | "")}>
          <option value="hurts">segments it hurts</option>
          <option value="helps">segments it helps</option>
          <option value="unclear">unclear segments</option>
          <option value="">all segments</option>
        </select>
      </label>
      {state === "ready" && items.length === 0 && (
        <p className="state" role="status">
          <strong>No segments {verdict ? `where treatment ${verdict === "unclear" ? "is unclear" : verdict}` : ""}.</strong>
          {verdict === "hurts" && " In this experiment there is no segment with statistically reliable harm. That is a finding, not a gap."}
        </p>
      )}
      {items.length > 0 && (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Segment</th>
                <th scope="col">Verdict</th>
                <th scope="col">Uplift</th>
                <th scope="col">95% interval</th>
                <th scope="col">Adjusted p</th>
                <th scope="col">Treated / control</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.definition}>
                  <th scope="row">{s.definition}</th>
                  <td>
                    <span className={`verdict ${s.verdict}`}>{VERDICT[s.verdict]}</span>
                  </td>
                  <td>{points(s.uplift)}</td>
                  <td>
                    {points(s.ci_low)} to {points(s.ci_high)}
                  </td>
                  <td>{s.p_adjusted < 0.001 ? "<0.001" : s.p_adjusted.toFixed(3)}</td>
                  <td>
                    {s.n_treated.toLocaleString()} / {s.n_control.toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {state === "loading" && (
        <p role="status" className="state">
          Loading segments…
        </p>
      )}
      {state === "error" && (
        <div className="state error" role="alert">
          <p>
            <strong>Could not load segments.</strong> {message}
          </p>
          <button onClick={() => void load(items.length ? cursor : null, !items.length)}>Try again</button>
        </div>
      )}
      {state === "ready" && cursor && <button onClick={() => void load(cursor, false)}>Show more segments</button>}
    </section>
  );
}

function PolicyBuilder({ champion, outcome, onUnauthorized }: { champion: Run; outcome: string; onUnauthorized: () => void }) {
  const [form, setForm] = useState({ budget: "1500", cost: "1", value: "20" });
  const [state, setState] = useState<{ status: "idle" | "working" | "done" | "error"; policy?: Policy; message?: string }>({ status: "idle" });
  const [download, setDownload] = useState("");
  const keys = useRef(new Map<string, string>()); // one idempotency key per distinct request, so a retry cannot create a second policy
  const result = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (state.status === "done") result.current?.focus();
  }, [state]);

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    const body = { run_id: champion.id, budget: Number(form.budget), cost_per_treatment: Number(form.cost), value_per_outcome: Number(form.value) };
    const signature = JSON.stringify(body);
    if (!keys.current.has(signature)) keys.current.set(signature, crypto.randomUUID());
    setState({ status: "working" });
    setDownload("");
    try {
      setState({ status: "done", policy: await createPolicy(body, keys.current.get(signature)!) });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return onUnauthorized();
      const message =
        err instanceof ApiError && err.status === 403
          ? "This token can view results but cannot create policies. Sign in with an admin token."
          : err instanceof Error
            ? err.message
            : "Unknown error";
      setState({ status: "error", message });
    }
  };
  const field = (name: keyof typeof form, text: string, hint: string, min: string) => (
    <label>
      {text}
      <input type="number" inputMode="decimal" required min={min} step="any" value={form[name]} onChange={(e) => setForm({ ...form, [name]: e.target.value })} />
      <small>{hint}</small>
    </label>
  );
  const p = state.policy;

  if (!champion.scored)
    return (
      <p className="state" role="status">
        <strong>The champion model has not been batch-scored yet.</strong> Run <code>uplift score</code>, then build a policy.
      </p>
    );
  return (
    <>
      <p className="lede">
        Treat the customers {label(champion.learner)} ranks highest, as many as the budget pays for, and never one whose predicted gain is worth less than the
        treatment costs.
      </p>
      <form className="policy" onSubmit={submit}>
        {field("budget", "Budget", "total to spend on this campaign", "0")}
        {field("cost", "Cost per treatment", "what one discount or e-mail costs", "0.0001")}
        {field("value", `Value of one ${outcome}`, "what the outcome is worth to you", "0.0001")}
        <button type="submit" disabled={state.status === "working"}>
          {state.status === "working" ? "Ranking and evaluating…" : "Build policy"}
        </button>
      </form>
      {state.status === "working" && (
        <p role="status" className="state">
          Ranking held-out customers and estimating the policy’s value from the experiment (300 bootstrap resamples)…
        </p>
      )}
      {state.status === "error" && (
        <p role="alert" className="state error">
          <strong>Could not build the policy.</strong> {state.message}
        </p>
      )}
      {state.status === "done" && p && (
        <section className="result" aria-labelledby="policy-result">
          <h3 id="policy-result" ref={result} tabIndex={-1}>
            Treat {p.n_targeted.toLocaleString()} of {p.population.toLocaleString()} customers
          </h3>
          <p className="meta">
            Spend {money(Number(p.spend))} of a {money(Number(p.budget))} budget.
            {p.n_targeted > 0 && p.min_uplift !== null && <> The last customer treated has a predicted uplift of {points(p.min_uplift)}.</>}
            {Number(p.spend) + Number(p.cost_per_treatment) <= Number(p.budget) && p.n_targeted < p.population && " Budget is left over on purpose: nobody else is predicted to pay back the cost."}
          </p>
          {p.evaluation.evaluable ? (
            <>
              <div className="tiles">
                {(
                  [
                    ["This policy", p.evaluation.profit_per_unit.policy],
                    ["Treat everyone", p.evaluation.profit_per_unit.treat_all],
                    ["Treat no one", p.evaluation.profit_per_unit.treat_none],
                  ] as const
                ).map(([name, v]) => (
                  <div className="tile" key={name}>
                    <span>{name}</span>
                    <strong>{money(v * 1000)}</strong>
                    <small>profit per 1,000 customers</small>
                  </div>
                ))}
              </div>
              <ul>
                {(
                  [
                    ["treating everyone", p.evaluation.policy_minus_treat_all],
                    ["treating no one", p.evaluation.policy_minus_treat_none],
                  ] as const
                ).map(([name, d]) => (
                  <li key={name}>
                    Versus {name}:{" "}
                    <strong>
                      {d.estimate >= 0 ? "+" : "−"}
                      {money(Math.abs(d.estimate * 1000))}
                    </strong>{" "}
                    per 1,000 customers (95% interval {money(d.ci95[0] * 1000)} to {money(d.ci95[1] * 1000)};{" "}
                    {d.ci95[0] > 0 ? "▲ reliably better" : d.ci95[1] < 0 ? "▼ reliably worse" : "◆ not distinguishable"})
                  </li>
                ))}
                {p.evaluation.share_of_treat_all_effect_captured !== null && (
                  <li>
                    Treats <strong>{pct(p.evaluation.share_of_population_treated)}</strong> of customers and captures{" "}
                    <strong>{pct(p.evaluation.share_of_treat_all_effect_captured)}</strong> of the effect of treating everyone.
                  </li>
                )}
              </ul>
              <p className="meta">Estimated from held-out experiment outcomes, not from the model’s own predictions.</p>
            </>
          ) : (
            <p className="state">
              <strong>Too small to evaluate.</strong> {p.evaluation.reason}.
            </p>
          )}
          <button
            disabled={p.n_targeted === 0}
            onClick={async () => {
              setDownload("Preparing the file…");
              try {
                await downloadPolicy(p.id);
                setDownload(`Downloaded ${p.n_targeted.toLocaleString()} customer ids.`);
              } catch (err) {
                setDownload(err instanceof Error ? `Download failed: ${err.message}` : "Download failed.");
              }
            }}
          >
            Download activation file (CSV)
          </button>
          <span role="status" className="meta">
            {" "}
            {download}
          </span>
        </section>
      )}
    </>
  );
}
