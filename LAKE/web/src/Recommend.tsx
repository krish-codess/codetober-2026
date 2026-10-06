// Recommendation panel: describe a workload, get every variant priced and ranked by the server.
// The cost model lives in one place (Python); this file only describes the workload and renders.
import { useEffect, useRef, useState } from "react";
import { ApiError, fmtSeconds, fmtUsd, recommend, type QueryDef, type Recommendation, type Workload } from "./api";

const CLASS_LABEL: Record<string, string> = {
  metadata: "Row counts", full_scan: "Full scans (all columns)", projection: "Column aggregates, no filter",
  filter_clustered: "Time-range filters (on the sort key)", filter_unclustered: "Filters on other columns",
  aggregate: "Group-bys",
};

interface Props { queries: QueryDef[]; stamp: string; onShow: (variant: string) => void }

export function RecommendPanel({ queries, stamp, onShow }: Props) {
  const classes = [...new Set(queries.map((q) => q.class))];
  const [w, setW] = useState<Workload>({
    dataset_gb: 500, queries_per_month: 20000, rewrites_per_month: 1, provider: "aws", engine: "serverless",
    objective: "balanced", mix: Object.fromEntries(classes.map((c) => [c, c === "full_scan" || c === "metadata" ? 1 : 3])),
  });
  const [result, setResult] = useState<Recommendation | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [busy, setBusy] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const firstRun = useRef(true);

  const mixEmpty = classes.every((c) => !(w.mix[c] > 0));
  const local: Record<string, string> = {};
  if (!(w.dataset_gb > 0)) local["body.dataset_gb"] = "Enter a size above 0 GB.";
  if (!(w.queries_per_month >= 0) || !Number.isInteger(w.queries_per_month)) local["body.queries_per_month"] = "Enter a whole number, 0 or more.";
  if (!(w.rewrites_per_month >= 0)) local["body.rewrites_per_month"] = "Enter 0 or more.";
  if (mixEmpty) local["body.mix"] = "Give at least one kind of query a weight above 0.";
  const invalid = Object.keys(local).length > 0;
  const fromServer = error instanceof ApiError ? Object.fromEntries(error.details.map((d) => [d.field, d.problem])) : {};
  const problems: Record<string, string> = { ...fromServer, ...local };

  useEffect(() => {
    if (invalid) return;
    const ctl = new AbortController();
    // Debounced so dragging a slider is one request, not forty. The first render asks immediately.
    const timer = setTimeout(() => {
      setBusy(true);
      recommend(w, stamp, ctl.signal)
        .then((r) => { setResult(r); setError(null); })
        .catch((e: unknown) => { if (!ctl.signal.aborted) setError(e instanceof Error ? e : new Error(String(e))); })
        .finally(() => { if (!ctl.signal.aborted) setBusy(false); });
    }, firstRun.current ? 0 : 250);
    firstRun.current = false;
    return () => { clearTimeout(timer); ctl.abort(); };
  }, [w, stamp, invalid, attempt]);

  const set = <K extends keyof Workload>(k: K, v: Workload[K]) => setW((prev) => ({ ...prev, [k]: v }));
  const number = (k: "dataset_gb" | "queries_per_month" | "rewrites_per_month", label: string, help: string, step: string) => {
    const problem = problems[`body.${k}`];
    return (
      <label className="field">{label}
        <input type="number" inputMode="decimal" min={0} step={step} value={Number.isNaN(w[k]) ? "" : w[k]}
          aria-invalid={problem ? true : undefined} aria-describedby={`${k}-help`}
          onChange={(e) => set(k, e.target.value === "" ? NaN : Number(e.target.value))} />
        <small id={`${k}-help`} className={problem ? "problem" : undefined}>{problem ?? help}</small>
      </label>
    );
  };
  const rows = result ? (showAll ? result.ranked : result.ranked.slice(0, 6)) : [];
  const usage = w.engine === "serverless" ? "scan" : "compute";

  return (
    <div className="recommend">
      <form onSubmit={(e) => e.preventDefault()} aria-label="Describe your workload" noValidate>
        <div className="fields">
          {number("dataset_gb", "Dataset size (GB as CSV)", "What the data weighs as uncompressed CSV.", "any")}
          {number("queries_per_month", "Queries per month", "All queries, across the mix below.", "1")}
          {number("rewrites_per_month", "Full rewrites per month", "0.1 = 10% new data a month.", "any")}
          <label className="field">Cloud
            <select value={w.provider} onChange={(e) => set("provider", e.target.value as Workload["provider"])}>
              <option value="aws">AWS (S3 + Athena)</option>
              <option value="gcp">GCP (GCS + BigQuery)</option>
              <option value="azure">Azure (Blob + Synapse)</option>
            </select>
          </label>
          <label className="field">Query engine billing
            <select value={w.engine} onChange={(e) => set("engine", e.target.value as Workload["engine"])}>
              <option value="serverless">Serverless: pay per byte scanned</option>
              <option value="self_hosted">Self-hosted: pay for CPU time</option>
            </select>
          </label>
          <label className="field">Optimise for
            <select value={w.objective} onChange={(e) => set("objective", e.target.value as Workload["objective"])}>
              <option value="balanced">Balance of cost and speed</option>
              <option value="cost">Lowest monthly cost</option>
              <option value="speed">Fastest queries</option>
            </select>
          </label>
        </div>
        <fieldset aria-describedby="mix-help">
          <legend>Query mix (relative weights)</legend>
          {classes.map((c) => (
            <label key={c} className="slider">
              <span>{CLASS_LABEL[c] ?? c}</span>
              <input type="range" min={0} max={10} step={1} value={w.mix[c] ?? 0}
                onChange={(e) => set("mix", { ...w.mix, [c]: Number(e.target.value) })} />
              <output>{w.mix[c] ?? 0}</output>
            </label>
          ))}
          <small id="mix-help" className={problems["body.mix"] ? "problem" : undefined}>
            {problems["body.mix"] ?? "Only the proportions matter: 3 and 1 means 75% and 25%."}
          </small>
        </fieldset>
      </form>

      <div className="recommendation" aria-live="polite" aria-busy={busy}>
        {error && !invalid && (
          <div className="banner error" role="alert">
            <span aria-hidden="true">✕</span>
            <span>Could not price this workload: {error.message}</span>
            <button type="button" onClick={() => setAttempt((n) => n + 1)}>Try again</button>
          </div>
        )}
        {invalid && <p className="state">Fix the highlighted field{Object.keys(local).length > 1 ? "s" : ""} to get a recommendation.</p>}
        {!result && !error && !invalid && <p className="state" role="status">Pricing {queries.length} queries across every variant…</p>}
        {result && !invalid && (
          <div className={busy || error ? "dim" : undefined}>
            <p className="pick">
              <span className="eyebrow">Recommended{busy ? " (updating…)" : ""}</span>
              <button type="button" className="link" onClick={() => onShow(result.pick)}>{result.pick}</button>
              <span className="price">{fmtUsd(result.ranked[0].total)}<small>/month</small></span>
            </p>
            <ul className="why">{result.why.map((line) => <li key={line}>{line}</li>)}</ul>
            <div className="scroll" tabIndex={0} role="region" aria-label="Every variant, priced">
              <table>
                <caption>Monthly cost on {result.price.label}</caption>
                <thead>
                  <tr><th scope="col">#</th><th scope="col">Variant</th><th scope="col" className="num">Total</th>
                    <th scope="col" className="num">Storage</th><th scope="col" className="num">{usage === "scan" ? "Scan" : "Compute"}</th>
                    <th scope="col" className="num">Requests</th><th scope="col" className="num">Write</th>
                    <th scope="col" className="num">Mix latency</th></tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={r.variant} className={i === 0 ? "top" : undefined}>
                      <td>{i + 1}</td>
                      <th scope="row"><button type="button" className="link" onClick={() => onShow(r.variant)}>{r.variant}</button></th>
                      <td className="num strong">{fmtUsd(r.total)}</td><td className="num">{fmtUsd(r.storage)}</td>
                      <td className="num">{fmtUsd(r[usage])}</td><td className="num">{fmtUsd(r.requests)}</td>
                      <td className="num">{fmtUsd(r.write)}</td><td className="num">{fmtSeconds(r.latency_s)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {result.ranked.length > 6 && (
              <button type="button" className="quiet" aria-expanded={showAll} onClick={() => setShowAll((s) => !s)}>
                {showAll ? "Show top 6" : `Show all ${result.ranked.length}`}
              </button>
            )}
            {result.excluded.length > 0 && (
              <p className="hint">Not ranked: {result.excluded.map((e) => `${e.variant} (${e.reason})`).join("; ")}.</p>
            )}
            <details><summary>What this estimate assumes</summary><ul>{result.assumptions.map((a) => <li key={a}>{a}</li>)}</ul></details>
          </div>
        )}
      </div>
    </div>
  );
}
