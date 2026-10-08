import { useEffect, useRef, useState } from "react";
import { type Package, type Point, type Resource, type Sensitivity, type Target, type Tradeoff, useResource } from "./api";
import { LegendMark, METRICS, type Metric, PRECISION, Scatter, SensitivityBars, family, num, pct } from "./Chart";

const mb = (bytes: number) => `${(bytes / 2 ** 20).toFixed(bytes < 10 * 2 ** 20 ? 2 : 0)} MB`;
const time = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** Loading, failed, and stale are the same three questions for every resource. */
function Status({ resources, what }: { resources: Resource<unknown>[]; what: string }) {
  const done = resources.filter((r) => !r.loading).length;
  const failed = resources.find((r) => r.error && r.data === null);
  const stale = resources.find((r) => r.error && r.data !== null);
  const retry = () => resources.forEach((r) => r.refresh());
  if (failed)
    return (
      <div className="notice error" role="alert">
        <strong>Could not load {what}.</strong> {failed.error}
        <button onClick={retry}>Try again</button>
      </div>
    );
  if (stale)
    return (
      <div className="notice warn" role="status">
        <strong>Showing saved data from {time(stale.fetchedAt!)}.</strong> The server could not be reached ({stale.error}).
        <button onClick={retry}>Try again</button>
      </div>
    );
  if (done < resources.length && resources.some((r) => r.data === null))
    return (
      <div className="notice" role="status">
        <progress value={done} max={resources.length} /> Loading {what}: {done} of {resources.length} requests done
      </div>
    );
  return null;
}

function Detail({ point, points, headingRef }: { point: Point; points: Point[]; headingRef: React.RefObject<HTMLHeadingElement | null> }) {
  const lineage: Point[] = [];
  for (let p: Point | undefined = point; p; p = points.find((q) => q.name === p!.parent)) lineage.unshift(p);
  const b = point.bench;
  return (
    <section className="card detail" aria-labelledby="detail-h">
      <h2 id="detail-h" tabIndex={-1} ref={headingRef}>
        {point.name}
      </h2>
      <p className={point.gate === "pass" ? "gate ok" : "gate bad"}>
        {point.gate === "pass" ? "✓ Passed the accuracy gate" : "✕ Failed the accuracy gate"}: {point.gate_reason}
      </p>
      <h3>How it got here</h3>
      <ol className="lineage">
        {lineage.map((p) => (
          <li key={p.name}>
            <strong>{p.name}</strong> <span className="muted">{p.technique === "none" ? "starting point" : p.technique}</span>
            <br />
            {pct(p.top1)} clean, {pct(p.top1_deploy)} under camera conditions
            {p.parent_delta !== null && (
              <>
                {" "}
                · {p.parent_delta >= 0 ? "+" : "−"}
                {Math.abs(p.parent_delta * 100).toFixed(1)} pt vs parent ({(p.parent_delta_lo! * 100).toFixed(1)} to {(p.parent_delta_hi! * 100).toFixed(1)}), {pct(p.parent_agree!)} same predictions
              </>
            )}
          </li>
        ))}
      </ol>
      <dl>
        <dt>Model</dt>
        <dd>
          {point.arch}, {PRECISION[point.precision]}, {(point.params / 1e6).toFixed(2)} M parameters, {mb(point.size_bytes)}
        </dd>
        <dt>On this target</dt>
        <dd>
          {b === null ? (
            "Not measured yet. Deploy the package and run the harness to add it."
          ) : (
            <>
              p50 {num(b.p50_ms)} ms, p95 {num(b.p95_ms)} ms, p99 {num(b.p99_ms)} ms over {b.runs} run{b.runs > 1 ? "s" : ""} on {b.cpu_model} ({b.threads || "default"} threads). On-device top-1 {pct(b.top1_device)}; {pct(b.agree_host)} of predictions match the build host.
            </>
          )}
        </dd>
        <dt>Power</dt>
        <dd>
          {b?.energy_mj != null
            ? `${num(b.energy_mj)} mJ per inference net of idle (${num(b.load_w!)} W under load, ${num(b.idle_w!)} W idle, sensor: ${b.power_source})`
            : `Not measured${b?.power_unavailable ? `: ${b.power_unavailable}` : "."}`}
        </dd>
      </dl>
    </section>
  );
}

function Explorer({ target, runtime, setRuntime }: { target: Target; runtime: string; setRuntime: (r: string) => void }) {
  const tradeoff = useResource<Tradeoff>(`/v1/tradeoff?target=${encodeURIComponent(target.name)}&runtime=${encodeURIComponent(runtime)}`);
  const sensitivity = useResource<Sensitivity>("/v1/sensitivity");
  const [metric, setMetric] = useState<Metric>("p50_ms");
  const [showFailed, setShowFailed] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const heading = useRef<HTMLHeadingElement | null>(null);
  const select = (name: string) => {
    setSelected(name);
    // Focus follows the selection, so keyboard and screen-reader users land on what just opened.
    requestAnimationFrame(() => heading.current?.focus());
  };
  const data = tradeoff.data;
  const status = <Status resources={[tradeoff]} what="the tradeoff" />;
  if (!data) return status;
  if (!data.run)
    return (
      <div className="notice">
        <strong>No pipeline run has been ingested yet.</strong> Run <code>python -m squeeze pipeline</code>, then <code>python -m squeeze ingest</code>.
      </div>
    );

  const measured = data.points.filter((p) => p.bench);
  const plottable = measured.filter((p) => p.bench![metric] != null && (showFailed || p.gate === "pass"));
  const unmeasured = data.points.length - measured.length;
  const noPower = measured.find((p) => p.bench!.energy_mj == null)?.bench?.power_unavailable;
  const families = [...new Map(data.points.map((p) => [family(p).key, family(p)])).values()].sort((a, b) => a.slot - b.slot);
  const point = data.points.find((p) => p.name === selected);
  const best = measured.filter((p) => p.gate === "pass" && p.meets_budget).sort((a, b) => b.top1 - a.top1)[0];

  return (
    <>
      {status}
      <section className="card" aria-labelledby="curve-h">
        <h2 id="curve-h">Accuracy against cost on {target.label}</h2>
        <p className="muted">
          Run {data.run.run_id} · teacher {pct(data.run.teacher_top1)} · a variant may ship if it stays within {data.run.budget_pt} points of the teacher
          {target.budget_p95_ms !== null && <> · latency budget p95 ≤ {target.budget_p95_ms} ms</>}
          {best && (
            <>
              {" "}
              · most accurate within budget: <strong>{best.name}</strong>
            </>
          )}
        </p>
        <div className="controls">
          <fieldset>
            <legend>Horizontal axis</legend>
            {(Object.keys(METRICS) as Metric[]).map((m) => (
              <label key={m}>
                <input type="radio" name="metric" checked={metric === m} onChange={() => setMetric(m)} /> {METRICS[m].label}
              </label>
            ))}
          </fieldset>
          <label>
            Runtime{" "}
            <select value={runtime} onChange={(e) => setRuntime(e.target.value)}>
              {[...new Set([...target.runtimes, ...data.runtimes_measured])].map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </label>
          <label>
            <input type="checkbox" checked={showFailed} onChange={(e) => setShowFailed(e.target.checked)} /> Show variants that failed the gate
          </label>
        </div>
        {measured.length === 0 ? (
          <div className="notice">
            <strong>
              Nothing has been measured on {target.label} with {runtime} yet.
            </strong>{" "}
            Accuracy is verified for all {data.points.length} variants (table below); latency and power appear once the harness has run on the device. See the Packages tab for the deploy command.
          </div>
        ) : plottable.length === 0 ? (
          <div className="notice">
            <strong>No {METRICS[metric].label.toLowerCase()} to plot.</strong>{" "}
            {metric === "energy_mj" ? `Power was not measured on this target${noPower ? `: ${noPower}` : "."}` : "Every measured variant failed the gate; tick the box above to see them."}
          </div>
        ) : (
          <>
            <ul className="legend" aria-label="Legend">
              {families.map((f) => (
                <li key={f.key}>
                  <LegendMark precision="fp32" slot={f.slot} /> {f.label}
                </li>
              ))}
              {(Object.keys(PRECISION) as Point["precision"][]).map((p) => (
                <li key={p}>
                  <LegendMark precision={p} slot={0} /> {PRECISION[p]}
                </li>
              ))}
              <li>
                <span className="key-line" aria-hidden="true" /> frontier: nothing is both faster and more accurate
              </li>
            </ul>
            <Scatter points={plottable} metric={metric} budget={target.budget_p95_ms} selected={selected} onSelect={select} />
          </>
        )}
        {(unmeasured > 0 || data.baselines.length > 0) && (
          <p className="muted">
            {measured.length > 0 && unmeasured > 0 && <>{unmeasured} variant{unmeasured > 1 ? "s are" : " is"} not measured on this target yet and only appear{unmeasured > 1 ? "" : "s"} in the table. </>}
            Baselines, far below this axis: {data.baselines.map((b) => `${b.name} ${pct(b.top1)}`).join(", ")}.
          </p>
        )}
      </section>

      <section className="card" aria-labelledby="table-h">
        <h2 id="table-h">Every variant, verified after every step</h2>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Variant</th>
                <th scope="col">Step</th>
                <th scope="col">Top-1 (95% interval)</th>
                <th scope="col">Camera conditions</th>
                <th scope="col">Gate</th>
                <th scope="col">Size</th>
                <th scope="col">p50 ms</th>
                <th scope="col">p95 ms</th>
                <th scope="col">mJ / inference</th>
              </tr>
            </thead>
            <tbody>
              {data.points.map((p) => (
                <tr key={p.name} className={p.name === selected ? "selected" : undefined}>
                  <th scope="row">
                    <button className="link" onClick={() => select(p.name)}>
                      {p.name}
                    </button>
                    {p.pareto && <em className="badge">frontier</em>}
                  </th>
                  <td>{p.technique === "none" ? "teacher" : p.technique}</td>
                  <td>
                    {pct(p.top1)} <span className="muted">({pct(p.top1_lo)} to {pct(p.top1_hi)})</span>
                  </td>
                  <td>{pct(p.top1_deploy)}</td>
                  <td>{p.gate === "pass" ? "✓ pass" : "✕ fail"}</td>
                  <td>{mb(p.size_bytes)}</td>
                  {p.bench ? (
                    <>
                      <td>{num(p.bench.p50_ms)}</td>
                      <td>
                        {num(p.bench.p95_ms)}
                        {p.meets_budget === false && <span title="over the latency budget"> ⚠ over</span>}
                      </td>
                      <td>{p.bench.energy_mj == null ? <span className="muted">no sensor</span> : num(p.bench.energy_mj)}</td>
                    </>
                  ) : (
                    <td colSpan={3} className="muted">
                      not measured on this target
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {point && <Detail point={point} points={data.points} headingRef={heading} />}

      <section className="card" aria-labelledby="sens-h">
        <h2 id="sens-h">Which layers quantization hurts</h2>
        {sensitivity.data ? (
          <>
            <p className="muted">
              Each layer of {sensitivity.data.variant} quantized to INT8 on its own, everything else float32: how far the outputs move (KL divergence, log scale) and the top-1 change on the dev split. The most sensitive of {sensitivity.data.rows.length} layers; the ones marked are left in float32 in the mixed-precision model.
            </p>
            <SensitivityBars rows={sensitivity.data.rows} />
          </>
        ) : sensitivity.loading ? (
          <p className="muted">Loading layer sensitivity…</p>
        ) : (
          <p className="muted">Not available: {sensitivity.error}</p>
        )}
      </section>
    </>
  );
}

function Packages({ targets }: { targets: Target[] }) {
  const packages = useResource<Package[]>("/v1/packages");
  const [copied, setCopied] = useState<string | null>(null);
  const copy = (text: string) => navigator.clipboard?.writeText(text).then(() => setCopied(text));
  if (!packages.data) return <Status resources={[packages]} what="packages" />;
  return (
    <>
      <Status resources={[packages]} what="packages" />
      {targets.map((t) => {
        const mine = packages.data!.filter((p) => p.target === t.name);
        return (
          <section className="card" key={t.name} aria-labelledby={`pk-${t.name}`}>
            <h2 id={`pk-${t.name}`}>{t.label}</h2>
            <p className="muted">
              {t.arch} · runtimes: {t.runtimes.join(", ")} · power sensor: {t.power_sensor} · {t.results} result{t.results === 1 ? "" : "s"} received
            </p>
            {mine.length === 0 && <p>No package has been built for this target.</p>}
            {mine.map((p) => (
              <article key={p.package_id} className="package">
                <h3>{p.filename}</h3>
                <p>
                  {mb(p.size_bytes)} · run {p.run_id} · commit {p.git_commit.slice(0, 8)} · dataset {p.dataset_version}
                </p>
                <p className="sha">
                  sha256 <code>{p.package_id}</code>{" "}
                  <button onClick={() => copy(p.package_id)} aria-label={`Copy sha256 of ${p.filename}`}>
                    {copied === p.package_id ? "Copied" : "Copy"}
                  </button>
                </p>
                {p.hosted ? (
                  <a className="download" href={`/v1/packages/${p.package_id}/download`} download>
                    Download package
                  </a>
                ) : (
                  <p className="muted">
                    Not hosted on this server (too large to commit). Rebuild it byte-for-byte with <code>python -m squeeze package --target {t.name}</code>.
                  </p>
                )}
                <p>
                  Deploy and benchmark: <code>deploy/deploy.sh {p.filename} user@device</code>
                </p>
                <ul>
                  {p.variants.map((v) => (
                    <li key={v.name}>
                      {v.name} <span className="muted">· {v.precision} · {mb(v.size_bytes)} · {pct(v.top1_host)} top-1</span>
                    </li>
                  ))}
                </ul>
              </article>
            ))}
          </section>
        );
      })}
    </>
  );
}

export function App() {
  const targets = useResource<Target[]>("/v1/targets");
  const read = () => new URLSearchParams(location.hash.slice(1));
  const [params, setParams] = useState(read);
  useEffect(() => {
    const onHash = () => setParams(read());
    const onFocus = () => targets.refresh(); // coming back to the tab is when new device results matter
    addEventListener("hashchange", onHash);
    addEventListener("focus", onFocus);
    return () => {
      removeEventListener("hashchange", onHash);
      removeEventListener("focus", onFocus);
    };
  }, [targets.refresh]);
  const go = (changes: Record<string, string>) => {
    const next = read();
    for (const [k, v] of Object.entries(changes)) next.set(k, v);
    location.hash = next.toString();
  };
  const view = params.get("view") === "packages" ? "packages" : "explore";
  const list = targets.data ?? [];
  // Default to the target with the most measurements: the page should open on evidence.
  const target = list.find((t) => t.name === params.get("target")) ?? [...list].sort((a, b) => b.results - a.results)[0];
  const runtime = params.get("runtime") ?? target?.runtimes[0] ?? "onnxruntime";

  return (
    <div className="viz-root">
      <header>
        <h1>squeeze</h1>
        <p>A ResNet-50 pruned, distilled and quantized for edge boards. Every point is verified accuracy and a measurement on the hardware named.</p>
        <nav aria-label="Views">
          <a href="#view=explore" aria-current={view === "explore" ? "page" : undefined} onClick={(e) => (e.preventDefault(), go({ view: "explore" }))}>
            Tradeoff explorer
          </a>
          <a href="#view=packages" aria-current={view === "packages" ? "page" : undefined} onClick={(e) => (e.preventDefault(), go({ view: "packages" }))}>
            Packages
          </a>
        </nav>
      </header>
      <main>
        <Status resources={[targets]} what="hardware targets" />
        {targets.data && view === "explore" && target && (
          <>
            <label className="target">
              Hardware target{" "}
              <select value={target.name} onChange={(e) => go({ target: e.target.value, runtime: list.find((t) => t.name === e.target.value)!.runtimes[0] })}>
                {list.map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.label} ({t.results === 0 ? "not measured yet" : `${t.results} results`})
                  </option>
                ))}
              </select>
            </label>
            <Explorer key={target.name} target={target} runtime={runtime} setRuntime={(r) => go({ runtime: r })} />
          </>
        )}
        {targets.data && view === "packages" && <Packages targets={list} />}
      </main>
    </div>
  );
}
