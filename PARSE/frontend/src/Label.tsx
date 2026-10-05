import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { ErrorBox, Loading, useTaxonomy } from "./App";
import { type QueueItem, type QueuePage, type TaxNode, api, pct } from "./api";

type Mode = "uncertain" | "review";
const PAGE = 20;
const REFILL_BELOW = 4;

/** Selecting a node selects its ancestors; removing one removes its descendants.
 *  Mirrors the database trigger so what the annotator sees is what gets stored. */
export function toggleNode(selected: ReadonlySet<number>, id: number, byId: Map<number, TaxNode>): Set<number> {
  const next = new Set(selected);
  if (next.has(id)) {
    const doomed = [id];
    while (doomed.length) {
      const d = doomed.pop() as number;
      next.delete(d);
      for (const n of byId.values()) if (n.parent_id === d && next.has(n.id)) doomed.push(n.id);
    }
  } else {
    for (let n = byId.get(id); n; n = n.parent_id == null ? undefined : byId.get(n.parent_id)) next.add(n.id);
  }
  return next;
}

const initialSelection = (item: QueueItem, mode: Mode) =>
  new Set(mode === "review" ? item.current_node_ids : item.suggestions.filter((s) => s.selected).map((s) => s.node_id));

export function Label() {
  const qc = useQueryClient();
  const taxonomy = useTaxonomy();
  const [mode, setMode] = useState<Mode>("uncertain");
  const [lang, setLang] = useState("");
  const [items, setItems] = useState<QueueItem[]>([]);
  const [edit, setEdit] = useState<{ id: number; set: Set<number> } | null>(null);
  const [skipped, setSkipped] = useState<Set<number>>(new Set());
  const [done, setDone] = useState(0);
  const [search, setSearch] = useState("");
  const [announce, setAnnounce] = useState("");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  // The queue is consumed locally, so it never goes stale on its own; it is refetched only
  // when the local buffer runs low or the filter changes.
  const queueKey = ["queue", mode, lang];
  const queue = useQuery({
    queryKey: queueKey,
    queryFn: () => api<QueuePage>(`/queue?mode=${mode}&limit=${PAGE}${lang ? `&lang=${lang}` : ""}`),
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: 0,
  });

  // ponytail: skipped items are filtered client-side from the top-20 page; someone who skips 20
  // in a row sees an empty buffer. Add a server-side "snooze" if skipping becomes a habit.
  // biome-ignore lint/correctness/useExhaustiveDependencies: refill only when a new page arrives
  useEffect(() => {
    if (queue.data) setItems(queue.data.items.filter((i) => !skipped.has(i.id)));
  }, [queue.data]);

  const byId = useMemo(() => new Map((taxonomy.data?.nodes ?? []).map((n) => [n.id, n])), [taxonomy.data]);
  const current = items[0];
  const currentId = current?.id;

  // The selection is derived, not synced: until the annotator edits it, it IS the item's initial
  // selection. An effect would leave one frame where a fresh item shows nothing checked - and a
  // fast Enter in that frame would save an empty label set.
  const selected = useMemo(
    () => (edit && edit.id === currentId ? edit.set : current ? initialSelection(current, mode) : new Set<number>()),
    [edit, current, currentId, mode],
  );
  const setSelected = (fn: (s: Set<number>) => Set<number>) =>
    currentId != null && setEdit({ id: currentId, set: fn(selected) });

  // Move focus to the new item's text - but only once it is really on screen. The queue can
  // arrive before the taxonomy, in which case the loading placeholder is still rendered when the
  // item id first changes and there is no heading to focus yet.
  const rendered = currentId != null && !taxonomy.isPending;
  // biome-ignore lint/correctness/useExhaustiveDependencies: move focus only when the item changes or first appears
  useEffect(() => {
    setSearch("");
    headingRef.current?.focus();
  }, [currentId, rendered]);

  const save = useMutation({
    mutationFn: ({ id, nodes }: { id: number; nodes: number[] }) =>
      api(`/items/${id}/annotation`, { method: "PUT", body: JSON.stringify({ node_ids: nodes }) }),
    // Optimistic: move on immediately; a PUT is idempotent so a retry cannot double-count.
    onMutate: ({ id }) => {
      const item = items.find((i) => i.id === id);
      setItems((list) => list.filter((i) => i.id !== id));
      return { item };
    },
    onSuccess: () => {
      setDone((d) => d + 1);
      setAnnounce("Saved. Next item loaded.");
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.item) setItems((list) => [ctx.item as QueueItem, ...list]);
      setAnnounce("Saving failed. The item is back on screen.");
    },
    onSettled: () => {
      if (items.length <= REFILL_BELOW) qc.invalidateQueries({ queryKey: queueKey });
    },
  });

  const submit = (nodes: number[]) => current && !save.isPending && save.mutate({ id: current.id, nodes });
  const skip = () => {
    if (!current) return;
    setSkipped((s) => new Set(s).add(current.id));
    setItems((list) => list.slice(1));
    setAnnounce("Skipped.");
  };

  // No dependency list on purpose: the handler is re-bound each render so it sees current state.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement;
      if (e.key === "Escape" && typing) (e.target as HTMLElement).blur();
      if (typing || e.metaKey || e.ctrlKey || e.altKey || !current) return;
      const n = Number(e.key);
      const suggestion = n >= 1 && n <= 9 ? current.suggestions[n - 1] : undefined;
      if (suggestion) setSelected((s) => toggleNode(s, suggestion.node_id, byId));
      else if (e.key === "Enter") submit([...selected]);
      else if (e.key === "s") skip();
      else if (e.key === "n") submit([]);
      else if (e.key === "/") searchRef.current?.focus();
      else return;
      e.preventDefault();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const matches = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (q.length < 2) return [];
    return (taxonomy.data?.nodes ?? [])
      .filter((n) => n.path.includes(q) || n.title.toLowerCase().includes(q))
      .slice(0, 8);
  }, [search, taxonomy.data]);

  const langs = ["en", "es", "de", "fr", "ja", "zh", "ru", "ar", "tr", "hi", "th", "sw"];
  const controls = (
    <div className="toolbar">
      <fieldset className="segmented">
        <legend className="sr-only">Queue</legend>
        {(["uncertain", "review"] as const).map((m) => (
          <button key={m} type="button" aria-pressed={mode === m} onClick={() => setMode(m)}>
            {m === "uncertain" ? "Most informative" : "Needs review"}
          </button>
        ))}
      </fieldset>
      <label>
        <span className="sr-only">Language</span>
        <select value={lang} onChange={(e) => setLang(e.target.value)}>
          <option value="">All languages</option>
          {langs.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
      </label>
      <span className="muted" data-testid="progress">
        {done} labelled this session{queue.data ? ` · ${Math.max(queue.data.remaining - done, 0)} in queue` : ""}
      </span>
    </div>
  );

  if (taxonomy.isPending || (queue.isPending && !items.length))
    return (
      <>
        {controls}
        <Loading what="the labelling queue" />
      </>
    );
  if (taxonomy.isError) return <ErrorBox error={taxonomy.error} onRetry={() => taxonomy.refetch()} />;
  if (queue.isError && !items.length)
    return (
      <>
        {controls}
        <ErrorBox error={queue.error} onRetry={() => queue.refetch()} />
      </>
    );

  if (!current)
    return (
      <>
        {controls}
        <div className="notice">
          {queue.isFetching ? (
            "Fetching more items…"
          ) : mode === "review" ? (
            <>
              <strong>Nothing needs review.</strong> Items appear here when a taxonomy change affects their labels.
            </>
          ) : queue.data?.active_model == null ? (
            <>
              <strong>No model yet.</strong> The queue is ordered by a model; an admin needs to run the first training.
            </>
          ) : (
            <>
              <strong>Queue empty{lang ? ` for “${lang}”` : ""}.</strong>{" "}
              {skipped.size > 0 ? "Everything left on this page was skipped. " : "Every item has been labelled. "}
              <button
                type="button"
                onClick={() => {
                  setSkipped(new Set());
                  queue.refetch();
                }}
              >
                Reload queue
              </button>
            </>
          )}
        </div>
      </>
    );

  const suggestedIds = new Set(current.suggestions.map((s) => s.node_id));
  const extra = [...selected].filter((id) => !suggestedIds.has(id) && byId.has(id));
  const stale = current.scored_by_model != null && current.scored_by_model !== queue.data?.active_model;

  return (
    <>
      {controls}
      <output className="sr-only" aria-live="polite">
        {announce}
      </output>
      {save.isError && <ErrorBox error={save.error} />}
      {queue.isError && <ErrorBox error={queue.error} onRetry={() => queue.refetch()} />}
      <article className="card item">
        <h2 ref={headingRef} tabIndex={-1} className="feedback" lang={current.lang ?? undefined}>
          {current.text}
        </h2>
        <p className="meta">
          <span className="tag">{current.lang ?? "language unknown"}</span>
          <span className="tag">{current.source}</span>
          {current.is_late && <span className="tag">late arrival</span>}
          {current.split === "test" && <span className="tag warn">reference item · admin only</span>}
          {current.confidence != null && <span>Model confidence {pct(current.confidence)}</span>}
        </p>
        {current.review_reason && (
          <p className="notice">
            <strong>Flagged by a taxonomy change</strong> ({current.review_reason}). Its current labels are
            pre-selected; pick a more specific one if it applies.
          </p>
        )}
        {stale && (
          <p className="notice">
            <strong>Older suggestions.</strong> These came from model v{current.scored_by_model}; v
            {queue.data?.active_model} is now active and will rescore shortly.
          </p>
        )}

        <fieldset className="suggestions">
          <legend>Model suggestions</legend>
          {current.suggestions.length === 0 && (
            <p className="muted">No suggestions for this item. Search the taxonomy below.</p>
          )}
          {current.suggestions.map((s, i) => {
            const node = byId.get(s.node_id);
            if (!node) return null;
            return (
              <label
                key={s.node_id}
                className="suggestion"
                style={{ paddingLeft: `${(node.depth - 1) * 1.1 + 0.5}rem` }}
              >
                <input
                  type="checkbox"
                  checked={selected.has(s.node_id)}
                  onChange={() => setSelected((sel) => toggleNode(sel, s.node_id, byId))}
                />
                {i < 9 && <kbd title={`Press ${i + 1} to toggle`}>{i + 1}</kbd>}
                <span className="path">
                  {node.title} <span className="muted">{node.path}</span>
                </span>
                {s.selected && <span className="tag">model pick</span>}
                <span className="prob">
                  <span className="bar" aria-hidden="true">
                    <span style={{ width: `${s.prob * 100}%` }} />
                  </span>
                  {pct(s.prob)}
                </span>
              </label>
            );
          })}
        </fieldset>

        {extra.length > 0 && (
          <p className="chips">
            Also selected:{" "}
            {extra.map((id) => (
              <button
                key={id}
                type="button"
                className="chip"
                onClick={() => setSelected((s) => toggleNode(s, id, byId))}
              >
                {byId.get(id)?.path} <span aria-hidden="true">×</span>
                <span className="sr-only">remove</span>
              </button>
            ))}
          </p>
        )}

        <div className="search">
          <label htmlFor="node-search">Add another category</label>
          <input
            id="node-search"
            ref={searchRef}
            type="search"
            placeholder="Type to search the taxonomy ( / )"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {search.trim().length >= 2 && matches.length === 0 && (
            <p className="muted">No category matches “{search}”.</p>
          )}
          <ul>
            {matches.map((n) => (
              <li key={n.id}>
                <button
                  type="button"
                  aria-pressed={selected.has(n.id)}
                  onClick={() => setSelected((s) => toggleNode(s, n.id, byId))}
                >
                  {n.path}
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="actions">
          <button type="button" className="primary" disabled={save.isPending} onClick={() => submit([...selected])}>
            Save &amp; next <kbd>Enter</kbd>
          </button>
          <button type="button" disabled={save.isPending} onClick={() => submit([])}>
            Nothing applies <kbd>N</kbd>
          </button>
          <button type="button" onClick={skip}>
            Skip <kbd>S</kbd>
          </button>
        </div>
      </article>
    </>
  );
}
