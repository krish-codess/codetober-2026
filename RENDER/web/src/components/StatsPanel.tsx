import type { MetaSummary } from "../lib/db";
import type { SelectionInfo } from "../lib/scene";
import { formatInt, formatShare, summarise } from "../lib/stats";
import type { Column } from "../lib/types";

export type MetaState = { status: "loading"; progress: number } | { status: "ready" } | { status: "error"; message: string };

interface Props {
  columns: Column[];
  total: number;
  selection: SelectionInfo | null;
  meta: MetaState;
  tokens: MetaSummary | null;
  onRetryMeta: () => void;
}

const TOP = 6;

export function StatsPanel({ columns, total, selection, meta, tokens, onRetryMeta }: Props) {
  if (!selection)
    return (
      <section aria-labelledby="sel-h" className="panel">
        <h2 id="sel-h">Selection</h2>
        <p className="muted">Nothing selected. Pick the lasso or box tool and drag over the points, or focus the plot and press Enter to select everything in view.</p>
      </section>
    );
  const summary = summarise(columns, selection.counts, total);
  return (
    <section aria-labelledby="sel-h" className="panel">
      <h2 id="sel-h">Selection</h2>
      {/* role=status: a screen reader hears the count change without the focus moving */}
      <p role="status" className="headline">
        <strong>{formatInt(selection.n)}</strong> of {formatInt(total)} points ({formatShare(total ? selection.n / total : 0)})
        {selection.live && <span className="tag">updating</span>}
      </p>
      {!selection.exact && (
        <p className="note" role="note">
          <strong>Partial count.</strong> Some tiles under this outline are not loaded, so this counts a sample. Zoom in, or wait for loading to finish.
        </p>
      )}
      <p className="muted small">
        Counted in {selection.latencyMs.toFixed(0)} ms ({selection.workerMs.toFixed(1)} ms in the worker), no server involved.
      </p>
      {selection.n === 0 ? (
        <p className="muted">No points inside the outline{selection.exact ? "" : " yet"}.</p>
      ) : (
        <>
          {summary.map((col) => (
            <div key={col.name} className="colsum">
              <h3>
                {col.name}
                <span className="muted small">
                  {" "}
                  purity {formatShare(col.purity ?? 0)} · entropy {(col.entropy ?? 0).toFixed(2)}
                </span>
              </h3>
              <table>
                <thead className="sr-only">
                  <tr>
                    <th>{col.name}</th>
                    <th>points</th>
                    <th>share of selection</th>
                    <th>lift over whole dataset</th>
                  </tr>
                </thead>
                <tbody>
                  {col.counts.slice(0, TOP).map((c) => (
                    <tr key={c.code}>
                      <th scope="row" title={c.label}>
                        {c.label}
                      </th>
                      <td>{formatInt(c.count)}</td>
                      <td>
                        <span className="bar" aria-hidden="true">
                          <span style={{ width: `${Math.max(2, c.share * 100)}%` }} />
                        </span>
                        {formatShare(c.share)}
                      </td>
                      <td title="share in the selection divided by share in the whole dataset">{c.lift == null ? "" : `×${c.lift.toFixed(c.lift < 10 ? 1 : 0)}`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {col.counts.length > TOP && <p className="muted small">and {col.counts.length - TOP} more</p>}
            </div>
          ))}
          <div className="colsum">
            <h3>tokens</h3>
            {meta.status === "loading" && (
              <p className="muted small">
                Loading the token table: <progress max={1} value={meta.progress} aria-label="token table download" /> {Math.round(meta.progress * 100)}%
              </p>
            )}
            {meta.status === "error" && (
              <p className="note" role="alert">
                Token statistics are unavailable ({meta.message}). The counts above do not depend on them. <button onClick={onRetryMeta}>Retry</button>
              </p>
            )}
            {meta.status === "ready" && selection.live && <p className="muted small">Release to list the tokens.</p>}
            {meta.status === "ready" && !selection.live && !tokens && <p className="muted small">Querying…</p>}
            {meta.status === "ready" && !selection.live && tokens && (
              <>
                {tokens.rank && (
                  <p className="small">
                    Frequency rank: median {formatInt(tokens.rank.median)} (quartiles {formatInt(tokens.rank.q25)} to {formatInt(tokens.rank.q75)}, range {formatInt(tokens.rank.min)} to {formatInt(tokens.rank.max)}). Mean length {(tokens.meanChars ?? 0).toFixed(1)} characters.
                  </p>
                )}
                <p className="small">Most frequent:</p>
                <ol className="tokens">
                  {tokens.top.map((t) => (
                    <li key={t.token}>
                      <bdi>{t.token}</bdi> <span className="muted">#{formatInt(t.rank)}</span>
                    </li>
                  ))}
                </ol>
                <p className="muted small">DuckDB-WASM, {tokens.ms.toFixed(0)} ms.</p>
              </>
            )}
          </div>
        </>
      )}
    </section>
  );
}
