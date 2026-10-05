import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ErrorBox, Loading } from "./App";
import { EfficiencyChart } from "./EfficiencyChart";
import { ApiError, type Efficiency, type NodeMetric, type NodeMetrics, api, pct } from "./api";

export const WEAK_F1 = 0.4;
export const MIN_SUPPORT = 20;
export const isWeak = (n: NodeMetric) => n.support >= MIN_SUPPORT && n.f1 < WEAK_F1;

function Bar({ value }: { value: number | null }) {
  return (
    <span className="cellbar">
      <span className="bar" aria-hidden="true">
        <span style={{ width: `${(value ?? 0) * 100}%` }} />
      </span>
      {pct(value)}
    </span>
  );
}

export function Performance() {
  const [lang, setLang] = useState("all");
  const [weakOnly, setWeakOnly] = useState(false);
  // Metrics only change when a new model is promoted; keep the previous language on screen while
  // the next one loads so the table does not flash empty.
  const metrics = useQuery({
    queryKey: ["node-metrics", lang],
    queryFn: () => api<NodeMetrics>(`/metrics/nodes?lang=${lang}`),
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
  });
  const eff = useQuery({
    queryKey: ["efficiency"],
    queryFn: () => api<Efficiency>("/metrics/efficiency"),
    staleTime: 5 * 60_000,
  });

  const rows = useMemo(() => {
    const nodes = metrics.data?.nodes ?? [];
    if (!weakOnly) return nodes;
    return nodes.filter(isWeak).sort((a, b) => a.f1 - b.f1);
  }, [metrics.data, weakOnly]);

  if (metrics.isPending) return <Loading what="model performance" />;
  if (metrics.isError) {
    if (metrics.error instanceof ApiError && metrics.error.status === 404)
      return (
        <div className="notice">
          <strong>No model yet.</strong> Performance appears here after the first training run.
        </div>
      );
    return <ErrorBox error={metrics.error} onRetry={() => metrics.refetch()} />;
  }

  const m = metrics.data;
  const o = m.overall;
  const weakCount = m.nodes.filter(isWeak).length;
  const langs = Object.entries(m.by_lang).sort((a, b) => b[1].hf1 - a[1].hf1);
  return (
    <>
      <section aria-labelledby="h-summary">
        <h2 id="h-summary">
          Model v{m.model_version} on held-out data {m.lang !== "all" && <span className="tag">{m.lang}</span>}
          {metrics.isPlaceholderData && <span className="muted"> updating…</span>}
        </h2>
        <dl className="tiles">
          <div>
            <dt>Hierarchical F1</dt>
            <dd>{o.hf1.toFixed(3)}</dd>
            {o.hf1_ci95 && (
              <dd className="muted">
                95% CI {o.hf1_ci95[0].toFixed(3)}–{o.hf1_ci95[1].toFixed(3)}
              </dd>
            )}
          </div>
          <div>
            <dt>Precision / recall</dt>
            <dd>
              {pct(o.precision)} / {pct(o.recall)}
            </dd>
          </div>
          <div>
            <dt>Whole label set right</dt>
            <dd>{pct(o.exact_match)}</dd>
          </div>
          <div>
            <dt>Weak nodes</dt>
            <dd>{weakCount}</dd>
            <dd className="muted">
              F1 under {WEAK_F1}, ≥{MIN_SUPPORT} examples
            </dd>
          </div>
        </dl>
      </section>

      <section aria-labelledby="h-eff">
        <h2 id="h-eff">Label efficiency</h2>
        {eff.isPending ? (
          <Loading what="the efficiency curve" />
        ) : eff.isError ? (
          <ErrorBox error={eff.error} onRetry={() => eff.refetch()} />
        ) : (
          <EfficiencyChart data={eff.data} />
        )}
      </section>

      <section aria-labelledby="h-lang">
        <h2 id="h-lang">By language</h2>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Language</th>
                <th scope="col">Items</th>
                <th scope="col">Hierarchical F1</th>
                <th scope="col">Whole set right</th>
                <th scope="col">
                  <span className="sr-only">Show nodes</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {langs.map(([code, s]) => (
                <tr key={code} aria-current={lang === code ? "true" : undefined}>
                  <th scope="row">{code}</th>
                  <td>{s.n.toLocaleString()}</td>
                  <td>
                    <Bar value={s.hf1} />
                  </td>
                  <td>{pct(s.exact_match)}</td>
                  <td>
                    <button
                      type="button"
                      aria-pressed={lang === code}
                      onClick={() => setLang(lang === code ? "all" : code)}
                    >
                      {lang === code ? "Show all" : "Nodes"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {m.calibration.routing && (
        <section aria-labelledby="h-cal">
          <h2 id="h-cal">Confidence and automatic routing</h2>
          <p className="muted">
            What accepting predictions without review would give at each threshold. With calibrated probabilities the
            share correct in a row is at least its threshold.
          </p>
          <div className="routing">
            {m.calibration.domain_routing && (
              <div className="scroll">
                <table>
                  <caption>
                    Route by top-level domain{" "}
                    <span className="muted">(calibration error {m.calibration.ece_domain?.toFixed(3)})</span>
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Probability ≥</th>
                      <th scope="col">Items routed</th>
                      <th scope="col">Routed correctly</th>
                    </tr>
                  </thead>
                  <tbody>
                    {m.calibration.domain_routing.map((r) => (
                      <tr key={r.threshold}>
                        <th scope="row">{pct(r.threshold)}</th>
                        <td>{pct(r.coverage, 1)}</td>
                        <td>{r.n === 0 ? "no items" : pct(r.accuracy, 1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {m.calibration.label_routing && (
              <div className="scroll">
                <table>
                  <caption>
                    Accept individual labels{" "}
                    <span className="muted">(calibration error {m.calibration.ece_label?.toFixed(3)})</span>
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Probability ≥</th>
                      <th scope="col">Labels correct</th>
                      <th scope="col">Share of true labels found</th>
                    </tr>
                  </thead>
                  <tbody>
                    {m.calibration.label_routing.map((r) => (
                      <tr key={r.threshold}>
                        <th scope="row">{pct(r.threshold)}</th>
                        <td>{r.n === 0 ? "no labels" : pct(r.precision, 1)}</td>
                        <td>{pct(r.recall, 1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="scroll">
              <table>
                <caption>
                  Accept the whole label set{" "}
                  <span className="muted">
                    (calibration error {m.calibration.ece_item?.toFixed(3)}, uncalibrated{" "}
                    {m.calibration.ece_item_uncalibrated?.toFixed(3)})
                  </span>
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Confidence ≥</th>
                    <th scope="col">Items accepted</th>
                    <th scope="col">Entire set correct</th>
                  </tr>
                </thead>
                <tbody>
                  {m.calibration.routing.map((r) => (
                    <tr key={r.threshold}>
                      <th scope="row">{pct(r.threshold)}</th>
                      <td>{pct(r.coverage, 1)}</td>
                      <td>{r.n === 0 ? "no items" : pct(r.exact_match, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      <section aria-labelledby="h-nodes">
        <h2 id="h-nodes">By taxonomy node</h2>
        <label className="check">
          <input type="checkbox" checked={weakOnly} onChange={(e) => setWeakOnly(e.target.checked)} /> Weak branches
          only ({weakCount})
        </label>
        {rows.length === 0 ? (
          <p className="notice">No weak nodes{lang !== "all" ? ` for “${lang}”` : ""} at the current thresholds.</p>
        ) : (
          <div className="scroll">
            <table className="nodes">
              <thead>
                <tr>
                  <th scope="col">Node</th>
                  <th scope="col">Examples</th>
                  <th scope="col">Precision</th>
                  <th scope="col">Recall</th>
                  <th scope="col">F1</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((n) => (
                  <tr key={n.node_id} className={isWeak(n) ? "weak" : undefined}>
                    <th scope="row" style={{ paddingLeft: weakOnly ? undefined : `${(n.depth - 1) * 1.1 + 0.5}rem` }}>
                      {weakOnly ? n.path : n.title}
                      {isWeak(n) && <span className="tag warn">▲ weak</span>}
                      {n.support === 0 && <span className="tag">no held-out examples</span>}
                    </th>
                    <td>{n.support.toLocaleString()}</td>
                    <td title={`95% CI ${pct(n.precision_ci[0])}–${pct(n.precision_ci[1])}`}>
                      {n.precision == null ? (
                        <span className="muted">never predicted</span>
                      ) : (
                        <Bar value={n.precision} />
                      )}
                    </td>
                    <td title={`95% CI ${pct(n.recall_ci[0])}–${pct(n.recall_ci[1])}`}>
                      {n.recall == null ? <span className="muted">–</span> : <Bar value={n.recall} />}
                    </td>
                    <td>{n.support === 0 && n.predicted === 0 ? <span className="muted">–</span> : n.f1.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
