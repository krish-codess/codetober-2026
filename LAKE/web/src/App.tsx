import { useMemo, useRef, useState } from "react";
import { fmtBytes, fmtPct, fmtSeconds, useResults, type Cell, type Results, type Variant } from "./api";
import { FORMATS, Shape, TradeoffChart } from "./Chart";
import { RecommendPanel } from "./Recommend";

const DAY = 86_400_000;

export function App() {
  const { state, refresh, refreshing } = useResults();
  return (
    <>
      <a className="skip" href="#main">Skip to results</a>
      <header>
        <h1>The Compression Bake-Off</h1>
        <p>One dataset, written in every format and codec, queried with a fixed workload. Measured in seconds, bytes read from storage, and dollars.</p>
      </header>
      <main id="main">
        {state.status === "loading" && (
          <div className="state" role="status">
            <p>Loading results… {Math.round(state.progress * 100)}%</p>
            <progress value={state.progress} max={1} aria-label="Results downloaded" />
          </div>
        )}
        {state.status === "empty" && (
          <div className="state" role="status">
            <h2>No benchmark has been run yet</h2>
            <p>{state.message}</p>
            <pre>docker compose --profile bench run --rm bench</pre>
            <button type="button" onClick={refresh} disabled={refreshing}>Check again</button>
          </div>
        )}
        {state.status === "error" && (
          <div className="banner error" role="alert">
            <span aria-hidden="true">✕</span>
            <span><strong>Results could not be loaded.</strong> {state.message}</span>
            <button type="button" onClick={refresh} disabled={refreshing} autoFocus>{refreshing ? "Retrying…" : "Retry"}</button>
          </div>
        )}
        {state.status === "ready" && <Dashboard data={state.data} staleReason={state.staleReason} refresh={refresh} refreshing={refreshing} />}
      </main>
    </>
  );
}

function Dashboard({ data, staleReason, refresh, refreshing }: { data: Results; staleReason: string | null; refresh: () => void; refreshing: boolean }) {
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useRef<HTMLHeadingElement>(null);
  // Selecting from a table or the recommendation moves focus to the detail panel, so keyboard and
  // screen-reader users land on what they asked for. Selecting inside the chart keeps focus there.
  const show = (id: string) => { setSelected(id); requestAnimationFrame(() => detail.current?.focus()); };
  const variant = data.variants.find((v) => v.id === selected) ?? null;
  const ageDays = Math.floor((Date.now() - Date.parse(data.generated_at)) / DAY);
  const smallest = data.variants.filter((v) => v.layout === "sorted").reduce((a, b) => (b.bytes < a.bytes ? b : a));
  const ev = data.env.eviction;

  return (
    <>
      {staleReason && (
        <div className="banner warn" role="alert">
          <span aria-hidden="true">!</span>
          <span><strong>Showing a saved copy.</strong> The latest results could not be fetched ({staleReason}).</span>
          <button type="button" onClick={refresh} disabled={refreshing}>{refreshing ? "Retrying…" : "Retry"}</button>
        </div>
      )}
      {data.partial && (
        <div className="banner warn" role="status">
          <span aria-hidden="true">!</span>
          <span><strong>Partial results.</strong> {data.missing.length} of {data.variants.length * data.queries.length} measurements failed or are missing
            ({data.missing.slice(0, 3).join(", ")}{data.missing.length > 3 ? ", …" : ""}). Affected variants are marked and left out of recommendations.</span>
        </div>
      )}
      {ageDays > 180 && (
        <div className="banner warn" role="status">
          <span aria-hidden="true">!</span>
          <span><strong>These results are {ageDays} days old.</strong> Reader and writer versions have probably moved on; re-run before relying on them.</span>
        </div>
      )}

      <section aria-labelledby="h-run">
        <h2 id="h-run">The run</h2>
        <dl className="tiles">
          <div><dt>Rows</dt><dd>{data.dataset.rows.toLocaleString("en-US")}</dd><small>{data.dataset.columns} columns, {data.dataset.name}</small></div>
          <div><dt>As CSV</dt><dd>{fmtBytes(data.dataset.csv_bytes)}</dd><small>the baseline every ratio is against</small></div>
          <div><dt>Smallest</dt><dd>{fmtBytes(smallest.bytes)}</dd><small>{smallest.id}, {smallest.ratio_vs_csv.toFixed(1)}× smaller</small></div>
          <div><dt>Variants × queries</dt><dd>{data.variants.length} × {data.queries.length}</dd><small>{data.origin === "local" ? "run on this machine" : "published run"}, {data.generated_at.slice(0, 10)}</small></div>
        </dl>
        <p className="hint">
          {Object.values(data.dataset.quarantined).reduce((a, b) => a + b, 0).toLocaleString("en-US")} of {data.dataset.landed.toLocaleString("en-US")} landed
          rows were quarantined before the bake-off. <button type="button" className="link" onClick={refresh} disabled={refreshing}>{refreshing ? "Checking…" : "Check for newer results"}</button>
        </p>
      </section>

      <section aria-labelledby="h-chart">
        <h2 id="h-chart">Size, write time, query time</h2>
        <TradeoffChart variants={data.variants} queries={data.queries} selected={selected} onSelect={setSelected} />
        <h3 ref={detail} tabIndex={-1} className="detail-title">{variant ? variant.id : "Select a variant"}</h3>
        {variant ? <Detail v={variant} data={data} /> : <p className="hint">Pick a mark in the chart or a row in any table to see every measurement behind it.</p>}
      </section>

      <section aria-labelledby="h-rec">
        <h2 id="h-rec">What should you store your data as?</h2>
        <RecommendPanel queries={data.queries} stamp={data.generated_at} onShow={show} />
      </section>

      <section aria-labelledby="h-table">
        <h2 id="h-table">Every variant</h2>
        <VariantTable data={data} selected={selected} onSelect={show} />
      </section>

      <section aria-labelledby="h-push">
        <h2 id="h-push">Did pushdown actually happen?</h2>
        <p className="hint">
          Bytes each query pulled from storage, as a share of the file. A reader that pushes projections down reads only the
          columns asked for. One that pushes predicates down reads less for the 1% filter than for the same columns unfiltered.
        </p>
        <PushdownTable data={data} onSelect={show} />
      </section>

      <section aria-labelledby="h-cols">
        <h2 id="h-cols">Compression ratio by column type</h2>
        <ColumnLab data={data} />
      </section>

      <section aria-labelledby="h-method">
        <h2 id="h-method">How this was measured</h2>
        <ul className="method">
          <li>{data.env.cpu}, {data.env.logical_cpus} logical CPUs, {data.env.ram_gb} GB RAM, {data.env.os}{data.env.in_container ? " (container)" : ""}.</li>
          <li>{Object.entries(data.env.versions ?? {}).map(([k, v]) => `${k} ${v}`).join(", ")}.</li>
          {data.env.settings && (
            <li>Per variant and query: up to {data.env.settings.cold_runs} cold runs, {data.env.settings.warmups} warm-up, up to {data.env.settings.warm_runs} warm
              runs, capped at {data.env.settings.cell_budget_s} s per kind (never fewer than 1 cold and 2 warm). Medians are shown; spread is in the detail panel.</li>
          )}
          {ev && (
            <li>
              <strong>Cold runs: {ev.effective ? "page cache eviction verified." : "page cache eviction NOT verified."}</strong>{" "}
              After asking the OS to drop the file, a sequential read ran at {ev.evicted_mb_s.toLocaleString("en-US")} MB/s against {ev.cached_mb_s.toLocaleString("en-US")} MB/s cached.
              {ev.effective ? "" : " On this machine a cold run means a fresh engine, not a cold disk."}
            </li>
          )}
          <li>Bytes read are counted by a filesystem wrapper in a separate, untimed execution, so the counter never touches a timing.</li>
        </ul>
      </section>
    </>
  );
}

function cellText(c: Cell | undefined, kind: "warm" | "cold" = "warm"): string {
  if (!c || !c[kind]) return c?.error ? "failed" : "no data";
  return fmtSeconds(c[kind]!.median_s) + (c.ok ? "" : " (wrong result)");
}

function Detail({ v, data }: { v: Variant; data: Results }) {
  return (
    <div className="detail">
      <p>
        {fmtBytes(v.bytes)} ({v.ratio_vs_csv.toFixed(2)}× smaller than CSV), written in {fmtSeconds(v.write_s)} wall / {fmtSeconds(v.write_cpu_s)} CPU.
        {v.chunks != null && ` ${v.chunks} ${v.format === "orc" ? "stripes" : "row groups"}.`} Read by {v.reader}.
        {v.layout !== "sorted" && " Rows deliberately shuffled: the control for predicate pushdown."}
      </p>
      <div className="scroll" tabIndex={0} role="region" aria-label={`Measurements for ${v.id}`}>
        <table>
          <thead>
            <tr><th scope="col">Query</th><th scope="col" className="num">Cold median</th><th scope="col" className="num">Warm median</th>
              <th scope="col" className="num">Warm min–max</th><th scope="col" className="num">Warm CV</th><th scope="col" className="num">Runs (cold/warm)</th>
              <th scope="col" className="num">Bytes read</th><th scope="col" className="num">Of file</th><th scope="col" className="num">Read calls</th></tr>
          </thead>
          <tbody>
            {data.queries.map((q) => {
              const c = v.queries[q.id];
              return (
                <tr key={q.id}>
                  <th scope="row" title={q.sql}>{q.id}</th>
                  {c?.warm && c.cold ? (
                    <>
                      <td className="num">{cellText(c, "cold")}</td><td className="num">{cellText(c)}</td>
                      <td className="num">{fmtSeconds(c.warm.min_s)}–{fmtSeconds(c.warm.max_s)}</td><td className="num">{fmtPct(c.warm.cv)}</td>
                      <td className="num">{c.cold.n}/{c.warm.n}</td><td className="num">{fmtBytes(c.bytes_read ?? 0)}</td>
                      <td className="num">{fmtPct(c.read_fraction ?? 0)}</td><td className="num">{(c.reads ?? 0).toLocaleString("en-US")}</td>
                    </>
                  ) : <td colSpan={8} className="bad">✕ {c?.error ?? "not measured"}</td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

type SortKey = "id" | "bytes" | "write_s" | string;

function VariantTable({ data, selected, onSelect }: { data: Results; selected: string | null; onSelect: (id: string) => void }) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "bytes", dir: 1 });
  const value = (v: Variant, key: SortKey): number | string =>
    key === "id" ? v.id : key === "bytes" ? v.bytes : key === "write_s" ? v.write_s : v.queries[key]?.warm?.median_s ?? Infinity;
  const rows = useMemo(() => [...data.variants].sort((a, b) => {
    const x = value(a, sort.key), y = value(b, sort.key);
    return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
  }), [data.variants, sort]);
  const head = (key: SortKey, label: string, num = true) => (
    <th scope="col" className={num ? "num" : undefined} aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
      <button type="button" className="sort" onClick={() => setSort((s) => ({ key, dir: s.key === key ? (-s.dir as 1 | -1) : 1 }))}>
        {label}<span aria-hidden="true">{sort.key === key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}</span>
      </button>
    </th>
  );
  return (
    <div className="scroll" tabIndex={0} role="region" aria-label="Every variant, sortable">
      <table>
        <caption>Warm median per query. Click a heading to sort.</caption>
        <thead>
          <tr>{head("id", "Variant", false)}{head("bytes", "Size")}<th scope="col" className="num">vs CSV</th>{head("write_s", "Write")}
            {data.queries.map((q) => head(q.id, q.id))}</tr>
        </thead>
        <tbody>
          {rows.map((v) => (
            <tr key={v.id} className={v.id === selected ? "top" : undefined}>
              <th scope="row">
                <button type="button" className="link" aria-pressed={v.id === selected} onClick={() => onSelect(v.id)}>
                  <svg viewBox="-10 -10 20 20" width="14" height="14" aria-hidden="true" className={`series ${v.format}`}><Shape format={v.format} r={6.5} /></svg>
                  {v.id}
                </button>
              </th>
              <td className="num">{fmtBytes(v.bytes)}</td><td className="num">{v.ratio_vs_csv.toFixed(2)}×</td><td className="num">{fmtSeconds(v.write_s)}</td>
              {data.queries.map((q) => {
                const c = v.queries[q.id];
                return <td key={q.id} className={`num${c?.warm && c.ok ? "" : " bad"}`} title={c?.error}>{c?.warm && c.ok ? "" : "✕ "}{cellText(c)}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Bar({ fraction }: { fraction: number | undefined }) {
  if (fraction == null) return <td className="num bad">no data</td>;
  return (
    <td className="num bar-cell">
      <span className="bar" style={{ width: `${Math.min(1, fraction) * 100}%` }} aria-hidden="true" />
      <span>{fmtPct(fraction)}</span>
    </td>
  );
}

function PushdownTable({ data, onSelect }: { data: Results; onSelect: (id: string) => void }) {
  const verdict = (b: boolean | null) => (b == null ? "no data" : b ? "✓ yes" : "✕ no");
  const filters = data.queries.filter((q) => q.control);
  return (
    <div className="scroll" tabIndex={0} role="region" aria-label="Pushdown by variant">
      <table>
        <thead>
          <tr><th scope="col">Variant</th><th scope="col">Reader</th>
            {data.queries.map((q) => <th key={q.id} scope="col" className="num">{q.id}</th>)}
            <th scope="col">Projection pushdown</th><th scope="col">Predicate pushdown</th>
            {filters.map((q) => <th key={q.id} scope="col" className="num">{q.id} skipped vs {q.control}</th>)}</tr>
        </thead>
        <tbody>
          {data.pushdown.map((p) => (
            <tr key={p.variant}>
              <th scope="row"><button type="button" className="link" onClick={() => onSelect(p.variant)}>{p.variant}</button></th>
              <td>{p.reader}</td>
              {data.queries.map((q) => <Bar key={q.id} fraction={p.read_fraction[q.id]} />)}
              <td className={p.projection_pushdown ? undefined : "bad"}>{verdict(p.projection_pushdown)}</td>
              <td className={p.predicate_pushdown ? undefined : "bad"}>{verdict(p.predicate_pushdown)}</td>
              {filters.map((q) => <td key={q.id} className="num">{p.pruned_vs_control[q.id] == null ? "no data" : fmtPct(Math.max(0, p.pruned_vs_control[q.id]))}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ColumnLab({ data }: { data: Results }) {
  const lab = data.columns.lab;
  const [format, setFormat] = useState("parquet");
  if (lab.cells.length === 0) return <p className="state" role="status">The column lab has not been run. Run <code>bakeoff columns</code>, then <code>bakeoff report</code>.</p>;
  const codecs = [...new Set(lab.cells.filter((c) => c.format === format).map((c) => c.codec))];
  const ratio = new Map(lab.cells.map((c) => [`${c.column}|${c.format}|${c.codec}`, c.ratio]));
  // One hue, light to dark, on a log scale: 1x is the surface, 100x and beyond is the darkest step.
  const step = (r: number) => Math.max(0, Math.min(5, Math.round(Math.log10(Math.max(r, 1)) * 2.5)));
  return (
    <>
      <div className="controls">
        <label>Format
          <select value={format} onChange={(e) => setFormat(e.target.value)}>
            {FORMATS.filter((f) => f.id !== "csv").map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
          </select>
        </label>
      </div>
      <p className="hint">
        {lab.rows.toLocaleString("en-US")} synthetic values per column, one file per column and codec. Ratio is in-memory Arrow bytes over file bytes:
        above 1× the file is smaller than the raw values. Darker is more compressed.
      </p>
      <div className="scroll" tabIndex={0} role="region" aria-label="Compression ratio by column and codec">
        <table className="heat">
          <thead>
            <tr><th scope="col">Column</th><th scope="col">Type</th><th scope="col" className="num">Distinct</th><th scope="col" className="num">Null</th>
              {codecs.map((c) => <th key={c} scope="col" className="num">{c}</th>)}</tr>
          </thead>
          <tbody>
            {lab.columns.map((col) => (
              <tr key={col.column}>
                <th scope="row">{col.column}</th><td>{col.type}</td>
                <td className="num">{col.distinct.toLocaleString("en-US")}</td><td className="num">{fmtPct(col.null_frac)}</td>
                {codecs.map((c) => {
                  const r = ratio.get(`${col.column}|${format}|${c}`);
                  return <td key={c} className="num" data-step={r == null ? undefined : step(r)}>{r == null ? "no data" : `${r >= 100 ? r.toFixed(0) : r.toFixed(1)}×`}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
