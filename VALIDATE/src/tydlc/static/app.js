// Failure viewer and invariant catalog. No build step: one ES module, also imported
// by the node tests (tests/js), which is why the DOM wiring sits behind `boot()`.

// --- pure logic (unit tested) ----------------------------------------------------------

export function esc(value) {
  return String(value).replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

// Generated data is hostile by design: NULL, "", "NULL" and " " must be told apart,
// so strings are shown JSON-quoted and a real NULL is styled differently.
export function cell(value) {
  if (value === null || value === undefined) return '<span class="null">NULL</span>';
  return esc(typeof value === "string" ? JSON.stringify(value) : value);
}

export function datasetHtml(dataset) {
  return Object.entries(dataset).map(([table, rows]) => {
    const head = `<h2>${esc(table)} <span class="badge note">${rows.length} ` +
      `row${rows.length === 1 ? "" : "s"}</span></h2>`;
    if (!rows.length) return `${head}<p class="sub">Empty. No rows are needed to reproduce.</p>`;
    const cols = Object.keys(rows[0]);
    return `${head}<div class="scroll"><table><thead><tr>` +
      cols.map((c) => `<th scope="col">${esc(c)}</th>`).join("") + "</tr></thead><tbody>" +
      rows.map((r) => "<tr>" + cols.map((c) => `<td class="mono">${cell(r[c])}</td>`).join("") +
        "</tr>").join("") + "</tbody></table></div>";
  }).join("");
}

export function percent(x) {
  return x === null || x === undefined ? "n/a" : `${(x * 100).toFixed(x > 0 && x < 0.1 ? 1 : 0)}%`;
}

const STATUS = { held: ["✓", "Held"], falsified: ["✗", "Falsified"], vacuous: ["–", "Not exercised"] };
export function statusBadge(status, knownBug) {
  const [icon, text] = STATUS[status] ?? ["?", status];
  const known = knownBug ? ' <span class="badge known">known bug</span>' : "";
  return `<span class="badge ${esc(status)}"><span aria-hidden="true">${icon}</span> ${text}</span>${known}`;
}

export function filterInvariants(items, { text = "", source = "", status = "" }) {
  const needle = text.trim().toLowerCase();
  const rank = { falsified: 0, held: 1, vacuous: 2 };
  return items
    .filter((i) => (!source || i.source === source) && (!status || i.status === status) &&
      (!needle || `${i.name} ${i.description}`.toLowerCase().includes(needle)))
    .sort((a, b) => rank[a.status] - rank[b.status] || a.name.localeCompare(b.name));
}

export function reproCommand(f) {
  return `tydlc run --engine ${f.engine} --seed ${f.seed} --max-examples ${f.max_examples}`;
}

export function runLabel(r) {
  const state = r.status === "completed" ? (r.gate_ok ? "gate passed" : "gate FAILED") : r.status;
  return `#${r.id} · ${r.engine} · seed ${r.seed} · ${state}`;
}

// Deliberate caching: a GET is reused for `ttl` ms. `invalidate()` (the Refresh
// button) drops freshness but keeps the last good value, so a failed refresh can
// still show data, flagged stale, instead of an empty screen.
export function createCache(fetchJson, ttl = 30000, now = () => Date.now()) {
  const entries = new Map();
  return {
    async get(url) {
      const hit = entries.get(url);
      if (hit && hit.fresh && now() - hit.at < ttl) return { data: hit.data, stale: false, at: hit.at };
      try {
        const data = await fetchJson(url);
        entries.set(url, { data, at: now(), fresh: true });
        return { data, stale: false, at: now() };
      } catch (error) {
        if (hit) return { data: hit.data, stale: true, at: hit.at, error };
        throw error;
      }
    },
    invalidate() { for (const e of entries.values()) e.fresh = false; },
  };
}

// --- browser wiring ------------------------------------------------------------------------

async function fetchJson(url) {
  let response;
  try {
    response = await fetch(url, { headers: { accept: "application/json" } });
  } catch {
    throw new Error("Cannot reach the server. Check your connection and retry.");
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const e = body?.error;
    throw new Error(e ? `${e.message} (${e.code}, ref ${e.correlation_id})` : `HTTP ${response.status}`);
  }
  return body;
}

export function boot(doc = document, win = window) {
  const cache = createCache(fetchJson);
  const main = doc.getElementById("main");
  const notice = doc.getElementById("notice");
  const select = doc.getElementById("run");
  let runs = [];
  let poll = null;
  let renderToken = 0;
  const filters = { text: "", source: "", status: "" };

  const currentRun = () => runs.find((r) => String(r.id) === select.value);
  const setNotice = (text) => { notice.textContent = text; };

  function show(html, { focus = false } = {}) {
    main.innerHTML = html;
    main.setAttribute("aria-busy", "false");
    if (focus) (main.querySelector("h1") ?? main).focus();
  }
  function loading(what) {
    main.setAttribute("aria-busy", "true");
    main.innerHTML = `<p class="state">Loading ${esc(what)}…</p>`;
  }
  function failed(error) {
    show(`<div class="state error" role="alert"><div>${esc(error.message)}</div>` +
      '<button type="button" id="retry">Try again</button></div>');
    doc.getElementById("retry").addEventListener("click", () => { cache.invalidate(); start(); });
  }
  function staleNotice(results) {
    const stale = results.find((r) => r.stale);
    setNotice(stale ? `Refresh failed (${stale.error.message}) Showing data from ` +
      `${new Date(stale.at).toLocaleTimeString()}.` : "");
  }

  // Everything behind a cursor: keep requesting until the server says there is no more.
  async function all(url) {
    const items = [];
    const results = [];
    let cursor = null;
    do {
      const sep = url.includes("?") ? "&" : "?";
      const r = await cache.get(cursor ? `${url}${sep}cursor=${encodeURIComponent(cursor)}` : url);
      results.push(r);
      items.push(...r.data.items);
      cursor = r.data.next_cursor;
    } while (cursor);
    return { items, results };
  }

  function runBanner(run) {
    if (run.status === "running") {
      return '<p class="why" role="status">This run is still in progress, started ' +
        `${new Date(run.started_at).toLocaleTimeString()}. Results appear when it finishes; ` +
        "this page checks every 5 seconds.</p>";
    }
    if (run.status === "failed") {
      return `<p class="state error" role="alert">This run did not finish: ${esc(run.error)}</p>`;
    }
    const rate = run.candidates ? percent(run.candidates_held / run.candidates) : "n/a";
    return '<ul class="stats">' +
      `<li><b>${run.examples}</b><span>datasets generated</span></li>` +
      `<li><b>${run.rows_generated}</b><span>adversarial rows</span></li>` +
      `<li><b>${(run.duration_ms / 1000).toFixed(1)}s</b><span>run time</span></li>` +
      `<li><b>${run.candidates_held}/${run.candidates}</b><span>discovered invariants held (${rate})</span></li>` +
      `<li><b>${run.gate_ok ? "Passed" : "Failed"}</b><span>CI gate</span></li></ul>`;
  }

  async function failuresView(run) {
    const { items, results } = await all(`api/failures?run_id=${run.id}&limit=200`);
    staleNotice(results);
    const head = `<h1 tabindex="-1">Failures in run #${run.id}</h1>` +
      `<p class="sub">Each falsified property with the smallest dataset that still breaks it.</p>` +
      runBanner(run);
    if (run.status !== "completed") return head;
    if (!items.length) {
      return `${head}<p class="state">No property was falsified in this run. ` +
        '<a href="#/catalog">See what held</a>.</p>';
    }
    const declared = items.filter((f) => f.source === "declared");
    const discovered = items.filter((f) => f.source === "discovered");
    const li = (f) => `<li><a href="#/failures/${f.id}"><div class="badges">` +
      statusBadge("falsified", f.known_bug) + `<span class="badge note">${esc(f.source)}</span></div>` +
      `<div class="name">${esc(f.property)}</div><div class="meta">${esc(f.description)} · ` +
      `fails ${percent(f.failed / (f.failed + f.passed))} of examples · minimal case ` +
      `${f.minimal_rows} row${f.minimal_rows === 1 ? "" : "s"}</div></a></li>`;
    const section = (title, list, note) => (list.length
      ? `<h2>${title} (${list.length})</h2><p class="sub">${note}</p><ul class="list">${list.map(li).join("")}</ul>`
      : "");
    return head +
      section("Declared properties", declared, "Written by hand. These gate CI.") +
      section("Discovered candidates", discovered,
        "Held on the seed data, broken by generated data. A bug or a coincidence of the sample.");
  }

  async function failureView(id) {
    const { data: f, stale, at, error } = await cache.get(`api/failures/${encodeURIComponent(id)}`);
    staleNotice([{ stale, at, error }]);
    const json = JSON.stringify(f.minimal_dataset, null, 2);
    const html = `<p><a href="#/failures">← All failures</a></p>` +
      `<h1 tabindex="-1" class="mono">${esc(f.property)}</h1><p class="sub">${esc(f.description)}</p>` +
      `<div class="row">${statusBadge("falsified", f.known_bug)}` +
      `<span class="badge note">${esc(f.source)}</span><span class="badge note">${esc(f.engine)}</span>` +
      `<span class="badge note">run #${f.run_id}</span></div>` +
      (f.known_bug ? `<h2>Why it fails</h2><p class="why">${esc(f.known_bug)}</p>` : "") +
      `<h2>Minimal reproducing dataset</h2><p class="sub">${f.minimal_rows} ` +
      `row${f.minimal_rows === 1 ? "" : "s"} in total. ` +
      (f.shrunk ? `Shrunk in ${f.shrink_calls} pipeline evaluations (${(f.shrink_ms / 1000).toFixed(1)}s); ` +
        "removing any row makes the property hold."
        : "Not shrunk: the shrink search did not rediscover this failure, so this is the first failing example as generated.") +
      ` It failed on ${percent(f.failed / (f.failed + f.passed))} of the ${f.failed + f.passed} examples that exercised it.</p>` +
      datasetHtml(f.minimal_dataset) +
      `<h2>Reproduce</h2><pre>${esc(reproCommand(f))}</pre>` +
      `<h2>As JSON</h2><div class="row"><button type="button" id="copy">Copy JSON</button>` +
      `<span id="copied" role="status"></span></div><pre>${esc(json)}</pre>`;
    return { html, after() {
      doc.getElementById("copy").addEventListener("click", async () => {
        const done = await win.navigator.clipboard?.writeText(json).then(() => true, () => false);
        doc.getElementById("copied").textContent = done ? "Copied." : "Copy failed; select the text below.";
      });
    } };
  }

  async function catalogView(run) {
    const { items, results } = await all(`api/runs/${run.id}/invariants?limit=200`);
    staleNotice(results);
    const head = `<h1 tabindex="-1">Invariant catalog, run #${run.id}</h1>` +
      '<p class="sub">Confidence is 1 − 3/n over the n examples that exercised the invariant ' +
      "(rule of three), and 0 once falsified.</p>" + runBanner(run);
    if (run.status !== "completed") return { html: head };
    if (!items.length) return { html: `${head}<p class="state">This run recorded no invariants.</p>` };
    const opt = (v, label, cur) => `<option value="${v}"${cur === v ? " selected" : ""}>${label}</option>`;
    const rows = filterInvariants(items, filters);
    const body = rows.map((i) => "<tr>" +
      `<td class="main"><div class="mono">${i.failure_id
        ? `<a href="#/failures/${i.failure_id}">${esc(i.name)}</a>` : esc(i.name)}</div>` +
      `<div class="desc">${esc(i.description)}</div></td>` +
      `<td data-th="Status">${statusBadge(i.status, i.known_bug)}</td>` +
      `<td data-th="Source">${esc(i.source)}</td>` +
      `<td data-th="Confidence" class="num"><meter min="0" max="1" value="${i.confidence}" ` +
      `aria-hidden="true"></meter> ${i.confidence.toFixed(3)}</td>` +
      `<td data-th="Held / failed / n.a." class="num">${i.passed} / ${i.failed} / ${i.vacuous}</td>` +
      `<td data-th="Falsified in" class="num">${i.history_falsified} of ${i.history_runs} runs</td></tr>`).join("");
    const html = head +
      '<form class="filters" id="filters" role="search">' +
      `<input type="search" name="text" placeholder="Filter by name or description" ` +
      `aria-label="Filter invariants" value="${esc(filters.text)}">` +
      `<select name="source" aria-label="Source">${opt("", "Any source", filters.source)}` +
      `${opt("declared", "Declared", filters.source)}${opt("discovered", "Discovered", filters.source)}</select>` +
      `<select name="status" aria-label="Status">${opt("", "Any status", filters.status)}` +
      `${opt("falsified", "Falsified", filters.status)}${opt("held", "Held", filters.status)}` +
      `${opt("vacuous", "Not exercised", filters.status)}</select></form>` +
      `<p class="sub" role="status">${rows.length} of ${items.length} invariants</p>` +
      (rows.length ? '<div class="scroll"><table class="cards"><thead><tr>' +
        '<th scope="col">Invariant</th><th scope="col">Status</th><th scope="col">Source</th>' +
        '<th scope="col">Confidence</th><th scope="col">Held / failed / n.a.</th>' +
        `<th scope="col">Falsified in</th></tr></thead><tbody>${body}</tbody></table></div>`
        : '<p class="state">No invariant matches these filters.</p>');
    return { html, after() {
      const form = doc.getElementById("filters");
      form.addEventListener("submit", (e) => e.preventDefault());
      form.addEventListener("input", (e) => {
        filters[e.target.name] = e.target.value;
        const caret = e.target.selectionStart;
        render({ keepFocus: e.target.name, caret });
      });
    } };
  }

  async function render({ focus = false, keepFocus = null, caret = null } = {}) {
    const token = ++renderToken;
    clearTimeout(poll);
    const [, view = "failures", id] = win.location.hash.split("/");
    for (const a of doc.querySelectorAll("nav a")) {
      a.dataset.view === view ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current");
    }
    const run = currentRun();
    if (!run) {
      return show('<h1 tabindex="-1">No runs yet</h1><p class="state">Nothing has been recorded. ' +
        "Start one with <code>tydlc run --store</code>, then press Refresh.</p>", { focus });
    }
    if (!keepFocus) loading(view === "catalog" ? "invariants" : "failures");
    try {
      let out;
      if (view === "catalog") out = await catalogView(run);
      else if (id) out = await failureView(id);
      else out = { html: await failuresView(run) };
      if (token !== renderToken) return;  // a newer navigation won
      show(out.html, { focus });
      out.after?.();
      if (keepFocus) {
        const el = doc.querySelector(`#filters [name="${keepFocus}"]`);
        el.focus();
        if (caret !== null && el.setSelectionRange) el.setSelectionRange(caret, caret);
      }
      if (run.status === "running") poll = setTimeout(() => { cache.invalidate(); start(); }, 5000);
    } catch (error) {
      if (token === renderToken) failed(error);
    }
  }

  async function start({ focus = false } = {}) {
    try {
      const { data, stale, at, error } = await cache.get("api/runs?limit=50");
      staleNotice([{ stale, at, error }]);
      runs = data.items;
      const keep = select.value;
      select.innerHTML = runs.map((r) => `<option value="${r.id}">${esc(runLabel(r))}</option>`).join("") ||
        "<option>No runs</option>";
      select.disabled = !runs.length;
      if (runs.some((r) => String(r.id) === keep)) select.value = keep;
      await render({ focus });
    } catch (error) {
      failed(error);
    }
  }

  select.addEventListener("change", () => {
    if (win.location.hash.split("/")[2]) win.location.hash = "#/failures";  // detail belongs to the old run
    else render({ focus: true });
  });
  doc.getElementById("refresh").addEventListener("click", () => { cache.invalidate(); start(); });
  win.addEventListener("hashchange", () => render({ focus: true }));
  start();
}

if (typeof document !== "undefined") boot();
