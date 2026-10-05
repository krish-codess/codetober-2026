import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { ErrorBox, Loading, useTaxonomy } from "./App";
import { type Change, type Job, type Role, type Stats, api, newKey } from "./api";

type Op = "add" | "split" | "rename" | "merge" | "move" | "retire";
const OPS: Record<Op, { title: string; effect: string }> = {
  split: {
    title: "Split a node into finer children",
    effect: "Existing labels stay; only items whose most specific label is this node are queued for review.",
  },
  merge: { title: "Merge one node into another", effect: "Labels move automatically. Nothing needs relabelling." },
  add: { title: "Add a child node", effect: "Items whose most specific label is the parent are queued for review." },
  rename: { title: "Rename a node", effect: "Labels are untouched." },
  move: {
    title: "Move a node under another parent",
    effect: "Labels follow; the old parent's label is flagged where nothing else justifies it.",
  },
  retire: { title: "Retire a node and its subtree", effect: "Items fall back to the parent label." },
};

function Jobs({ canRun }: { canRun: boolean }) {
  const qc = useQueryClient();
  // Poll only while something is in flight; otherwise this list is static.
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<{ items: Job[] }>("/jobs?limit=5"),
    refetchInterval: (q) =>
      q.state.data?.items.some((j) => j.status === "queued" || j.status === "running") ? 1500 : false,
    staleTime: 0,
  });
  const retrain = useMutation({
    // One key per click: a double-click or a network retry cannot queue two jobs.
    mutationFn: (key: string) =>
      api<Job>("/jobs", { method: "POST", body: JSON.stringify({ kind: "retrain" }), idempotencyKey: key }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const running = jobs.data?.items.some((j) => j.status === "queued" || j.status === "running") ?? false;
  // When the last job finishes, everything a new model changes is refetched - once.
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running)
      for (const k of ["node-metrics", "efficiency", "queue", "stats", "taxonomy"])
        qc.invalidateQueries({ queryKey: [k] });
    wasRunning.current = running;
  }, [running, qc]);
  return (
    <section aria-labelledby="h-jobs">
      <h2 id="h-jobs">Training jobs</h2>
      {canRun && (
        <button
          type="button"
          className="primary"
          disabled={retrain.isPending || running}
          onClick={() => retrain.mutate(newKey())}
        >
          {running ? "A job is in progress" : "Retrain now"}
        </button>
      )}
      {retrain.isError && <ErrorBox error={retrain.error} />}
      {jobs.isPending ? (
        <Loading what="jobs" />
      ) : jobs.isError ? (
        <ErrorBox error={jobs.error} onRetry={() => jobs.refetch()} />
      ) : jobs.data.items.length === 0 ? (
        <p className="muted">No jobs have run yet. Retraining also runs on a schedule when enough new labels arrive.</p>
      ) : (
        <ul className="jobs">
          {jobs.data.items.map((j) => (
            <li key={j.id}>
              <span>
                #{j.id} {j.kind} · <strong>{j.status}</strong> · by {j.requested_by}
              </span>
              {(j.status === "running" || j.status === "queued") && (
                <label>
                  <progress value={j.progress} max={1} /> {Math.round(j.progress * 100)}% — {j.stage}
                </label>
              )}
              {j.status === "failed" && (
                <span className="field-error">
                  Failed after {j.attempts} attempt(s): {j.error}
                </span>
              )}
              {j.status === "queued" && j.error && <span className="muted">Retrying after: {j.error}</span>}
              {j.status === "succeeded" && j.result && "hf1" in j.result && (
                <span className="muted">
                  model v{String(j.result.model_version)} · hF1 {Number(j.result.hf1).toFixed(3)} ·{" "}
                  {j.result.promoted ? "promoted" : "rejected by the regression gate"}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ChangeForm() {
  const qc = useQueryClient();
  const [op, setOp] = useState<Op>("split");
  const [f, setF] = useState({ path: "", target: "", name: "", title: "", children: "" });
  const [key, setKey] = useState(newKey);
  const [done, setDone] = useState<Change | null>(null);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const change = useMutation({
    mutationFn: (body: unknown) =>
      api<Change>("/taxonomy/changes", { method: "POST", body: JSON.stringify(body), idempotencyKey: key }),
    onSuccess: (c) => {
      setDone(c);
      setKey(newKey()); // next submit is a new change; retries of this one reused the same key
      setF({ path: "", target: "", name: "", title: "", children: "" });
      for (const k of ["taxonomy", "changes", "jobs", "queue", "stats"]) qc.invalidateQueries({ queryKey: [k] });
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    setDone(null);
    const slug = (s: string) =>
      s
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_|_$/g, "");
    const body: Record<Op, unknown> = {
      split: {
        op,
        path: f.path.trim(),
        children: f.children
          .split(",")
          .filter((c) => c.trim())
          .map((c) => ({ name: slug(c), title: c.trim() })),
      },
      merge: { op, source: f.path.trim(), target: f.target.trim() },
      add: { op, parent: f.path.trim() || null, name: slug(f.name), title: f.title.trim() || f.name.trim() },
      rename: { op, path: f.path.trim(), name: slug(f.name), title: f.title.trim() || null },
      move: { op, path: f.path.trim(), new_parent: f.target.trim() || null },
      retire: { op, path: f.path.trim() },
    };
    change.mutate(body[op]);
  }
  const needs = {
    target: op === "merge" || op === "move",
    name: op === "add" || op === "rename",
    children: op === "split",
  };
  return (
    <form className="card" onSubmit={submit} aria-labelledby="h-change">
      <h2 id="h-change">Change the taxonomy</h2>
      <label htmlFor="op">Operation</label>
      <select id="op" value={op} onChange={(e) => setOp(e.target.value as Op)}>
        {(Object.keys(OPS) as Op[]).map((o) => (
          <option key={o} value={o}>
            {OPS[o].title}
          </option>
        ))}
      </select>
      <p className="muted">{OPS[op].effect}</p>
      <label htmlFor="path">
        {op === "add"
          ? "Parent path (empty for a new top-level domain)"
          : op === "merge"
            ? "Node to retire (path)"
            : "Node path"}
      </label>
      <input
        id="path"
        list="paths"
        value={f.path}
        onChange={set("path")}
        required={op !== "add"}
        placeholder="hotel/rooms/comfort"
      />
      {needs.target && (
        <>
          <label htmlFor="target">
            {op === "merge" ? "Merge into (path)" : "New parent path (empty for top level)"}
          </label>
          <input id="target" list="paths" value={f.target} onChange={set("target")} required={op === "merge"} />
        </>
      )}
      {needs.name && (
        <>
          <label htmlFor="name">Name</label>
          <input id="name" value={f.name} onChange={set("name")} required />
          <label htmlFor="title">Display title (optional)</label>
          <input id="title" value={f.title} onChange={set("title")} />
        </>
      )}
      {needs.children && (
        <>
          <label htmlFor="children">New children, comma separated (at least two)</label>
          <input id="children" value={f.children} onChange={set("children")} required placeholder="Bed, Noise" />
        </>
      )}
      <button type="submit" className="primary" disabled={change.isPending}>
        {change.isPending ? "Applying…" : "Apply change"}
      </button>
      {change.isError && <ErrorBox error={change.error} />}
      {done && (
        <output className="notice ok">
          <strong>✓ Taxonomy is now v{done.version}.</strong> {done.labels_remapped} labels carried over automatically,{" "}
          {done.labels_flagged} queued for review. A retrain has been queued.
        </output>
      )}
    </form>
  );
}

export function TaxonomyAdmin({ role }: { role: Role }) {
  const taxonomy = useTaxonomy();
  const stats = useQuery({ queryKey: ["stats"], queryFn: () => api<Stats>("/stats"), staleTime: 30_000 });
  const changes = useQuery({
    queryKey: ["changes"],
    queryFn: () => api<{ items: Change[] }>("/taxonomy/changes?limit=8"),
    staleTime: 5 * 60_000,
  });
  const [filter, setFilter] = useState("");
  if (taxonomy.isPending) return <Loading what="the taxonomy" />;
  if (taxonomy.isError) return <ErrorBox error={taxonomy.error} onRetry={() => taxonomy.refetch()} />;
  const q = filter.trim().toLowerCase();
  const nodes = taxonomy.data.nodes.filter((n) => !q || n.path.includes(q));
  return (
    <>
      <section aria-labelledby="h-data">
        <h2 id="h-data">Data</h2>
        {stats.isPending ? (
          <Loading what="statistics" />
        ) : stats.isError ? (
          // Partial data: the rest of the page is still usable without the statistics.
          <ErrorBox error={stats.error} onRetry={() => stats.refetch()} />
        ) : (
          <dl className="tiles">
            <div>
              <dt>Labelled</dt>
              <dd>
                {stats.data.labelled.toLocaleString()}{" "}
                <span className="muted">of {stats.data.pool.toLocaleString()}</span>
              </dd>
            </div>
            <div>
              <dt>Awaiting review</dt>
              <dd>{stats.data.needs_review.toLocaleString()}</dd>
            </div>
            <div>
              <dt>Quarantined</dt>
              <dd>
                {Object.values(stats.data.quarantined)
                  .reduce((a, b) => a + b, 0)
                  .toLocaleString()}
              </dd>
              <dd className="muted">
                {Object.entries(stats.data.quarantined)
                  .slice(0, 3)
                  .map(([r, n]) => `${r.replace(/_/g, " ")} ${n}`)
                  .join(" · ")}
              </dd>
            </div>
            <div>
              <dt>Held-out items</dt>
              <dd>{stats.data.test.toLocaleString()}</dd>
            </div>
          </dl>
        )}
      </section>
      <Jobs canRun={role === "admin"} />
      {role === "admin" && <ChangeForm />}
      <section aria-labelledby="h-tax">
        <h2 id="h-tax">
          Taxonomy v{taxonomy.data.version} <span className="muted">· {taxonomy.data.nodes.length} nodes</span>
        </h2>
        <label htmlFor="tax-filter" className="sr-only">
          Filter nodes
        </label>
        <input
          id="tax-filter"
          type="search"
          placeholder="Filter by path"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <datalist id="paths">
          {taxonomy.data.nodes.map((n) => (
            <option key={n.id} value={n.path} />
          ))}
        </datalist>
        {nodes.length === 0 ? (
          <p className="notice">No node matches “{filter}”.</p>
        ) : (
          <div className="scroll tall">
            <table>
              <thead>
                <tr>
                  <th scope="col">Node</th>
                  <th scope="col">Labelled items</th>
                  <th scope="col">Awaiting review</th>
                </tr>
              </thead>
              <tbody>
                {nodes.map((n) => (
                  <tr key={n.id}>
                    <th
                      scope="row"
                      style={{ paddingLeft: q ? undefined : `${(n.depth - 1) * 1.1 + 0.5}rem` }}
                      title={n.path}
                    >
                      {q ? n.path : n.title}
                    </th>
                    <td>{n.n_labels}</td>
                    <td>{n.n_review > 0 ? <span className="tag warn">▲ {n.n_review}</span> : "0"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section aria-labelledby="h-log">
        <h2 id="h-log">Change log</h2>
        {changes.isPending ? (
          <Loading what="changes" />
        ) : changes.isError ? (
          <ErrorBox error={changes.error} onRetry={() => changes.refetch()} />
        ) : (
          <ol className="log">
            {changes.data.items.map((c) => (
              <li key={c.version}>
                <strong>v{c.version}</strong> {c.op}{" "}
                <code>{String(c.params.path ?? c.params.source ?? c.params.name ?? "")}</code> by {c.actor} —{" "}
                {c.labels_remapped} carried over, {c.labels_flagged} flagged
              </li>
            ))}
          </ol>
        )}
      </section>
    </>
  );
}
