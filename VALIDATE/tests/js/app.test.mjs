// Component tests for the viewer's logic. Run with: node --test tests/js
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  cell, createCache, datasetHtml, esc, filterInvariants, percent, reproCommand, runLabel,
  statusBadge,
} from "../../src/tydlc/static/app.js";

test("generated strings cannot inject markup", () => {
  const hostile = `<img src=x onerror="alert(1)">'`;
  assert.equal(esc(hostile), "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&#39;");
  const html = datasetHtml({ raw_customers: [{ id: 1, first_name: hostile }] });
  assert.ok(!html.includes("<img"));
});

test("NULL, empty string, the text NULL and whitespace are distinguishable", () => {
  const shown = [null, "", "NULL", " ", 0].map(cell);
  assert.equal(new Set(shown).size, 5);
  assert.equal(shown[0], '<span class="null">NULL</span>');
  assert.equal(shown[1], "&quot;&quot;");
  assert.equal(shown[2], "&quot;NULL&quot;");
  assert.equal(cell("line\nbreak"), "&quot;line\\nbreak&quot;");
});

test("dataset renders one table per source table, and says when one is empty", () => {
  const html = datasetHtml({
    raw_orders: [{ id: 0, status: "placed" }],
    raw_payments: [],
  });
  assert.match(html, /raw_orders <span class="badge note">1 row<\/span>/);
  assert.match(html, /<th scope="col">status<\/th>/);
  assert.match(html, /raw_payments <span class="badge note">0 rows<\/span>/);
  assert.match(html, /No rows are needed to reproduce/);
});

test("status is carried by text and symbol, not colour alone", () => {
  const badge = statusBadge("falsified", "explained");
  assert.match(badge, /Falsified/);
  assert.match(badge, /aria-hidden="true">✗/);
  assert.match(badge, /known bug/);
  assert.doesNotMatch(statusBadge("held", null), /known bug/);
  assert.match(statusBadge("vacuous", null), /Not exercised/);
});

test("percent keeps small frequencies visible", () => {
  assert.equal(percent(0.77), "77%");
  assert.equal(percent(0.004), "0.4%");
  assert.equal(percent(0), "0%");
  assert.equal(percent(null), "n/a");
});

const ITEMS = [
  { name: "b_held", description: "rows match", source: "declared", status: "held" },
  { name: "a_held", description: "unique id", source: "discovered", status: "held" },
  { name: "z_broken", description: "amount not null", source: "declared", status: "falsified" },
  { name: "idle", description: "never ran", source: "discovered", status: "vacuous" },
];

test("catalog lists falsified first, then by name", () => {
  assert.deepEqual(filterInvariants(ITEMS, {}).map((i) => i.name),
    ["z_broken", "a_held", "b_held", "idle"]);
});

test("catalog filters combine", () => {
  const names = (f) => filterInvariants(ITEMS, f).map((i) => i.name);
  assert.deepEqual(names({ source: "declared" }), ["z_broken", "b_held"]);
  assert.deepEqual(names({ status: "held", source: "discovered" }), ["a_held"]);
  assert.deepEqual(names({ text: "  AMOUNT " }), ["z_broken"]);
  assert.deepEqual(names({ text: "nothing matches" }), []);
});

test("repro command and run label", () => {
  assert.equal(reproCommand({ engine: "postgres", seed: 28, max_examples: 200 }),
    "tydlc run --engine postgres --seed 28 --max-examples 200");
  assert.match(runLabel({ id: 3, engine: "duckdb", seed: 1, status: "completed", gate_ok: false }),
    /gate FAILED/);
  assert.match(runLabel({ id: 3, engine: "duckdb", seed: 1, status: "running" }), /running/);
});

test("cache does not refetch within the ttl", async () => {
  let calls = 0;
  let clock = 0;
  const cache = createCache(async () => ++calls, 1000, () => clock);
  assert.equal((await cache.get("/x")).data, 1);
  clock = 999;
  assert.equal((await cache.get("/x")).data, 1);
  assert.equal(calls, 1);
  clock = 1001;
  assert.equal((await cache.get("/x")).data, 2);
  assert.equal((await cache.get("/y")).data, 3);
});

test("invalidate forces a refetch", async () => {
  let calls = 0;
  const cache = createCache(async () => ++calls, 1000, () => 0);
  await cache.get("/x");
  cache.invalidate();
  assert.equal((await cache.get("/x")).data, 2);
});

test("a failed refresh serves the last good value, marked stale", async () => {
  let fail = false;
  let clock = 5;
  const cache = createCache(async () => {
    if (fail) throw new Error("offline");
    return "good";
  }, 1000, () => clock);
  await cache.get("/x");
  fail = true;
  cache.invalidate();
  clock = 50;
  const result = await cache.get("/x");
  assert.deepEqual({ data: result.data, stale: result.stale, at: result.at },
    { data: "good", stale: true, at: 5 });
  assert.equal(result.error.message, "offline");
});

test("with nothing cached, a failure is an error, not empty data", async () => {
  const cache = createCache(async () => { throw new Error("offline"); });
  await assert.rejects(cache.get("/x"), /offline/);
});
