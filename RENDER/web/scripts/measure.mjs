// Measure a running deployment in a real browser and write the numbers the README quotes.
//
//   node scripts/measure.mjs <base-url> <results.json> [screenshot-dir]
//
// It drives the page through window.__dots (the same Scene the user drives): first paint, a
// scripted pan/zoom with every frame interval recorded, lasso selections of growing size, and a
// roam under a small point budget to show memory stays bounded. HEADLESS=1 hides the window;
// the GPU string in the output says what actually rendered.
import { chromium } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

const [base, out, shots] = process.argv.slice(2);
if (!base || !out) {
  console.error("usage: node scripts/measure.mjs <base-url> <results.json> [screenshot-dir]");
  process.exit(2);
}
const url = base.endsWith("/") ? base : base + "/";
const browser = await chromium.launch({
  channel: process.env.PW_CHANNEL ?? "chrome",
  headless: !!process.env.HEADLESS,
  args: ["--ignore-gpu-blocklist", "--enable-gpu", "--use-angle=d3d11", "--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
});

const debug = (page) => page.evaluate(() => window.__dots.debug());
const settled = async (page, timeout = 180_000) => {
  await page.waitForFunction(() => {
    const s = window.__dots?.debug().status;
    return !!s && s.pointsDrawn > 0 && s.tilesLoaded === s.tilesWanted;
  }, null, { timeout });
};
const shot = async (page, name) => {
  if (!shots) return;
  mkdirSync(shots, { recursive: true });
  await page.screenshot({ path: join(shots, name) });
};

async function open(viewport, query = "", colorScheme = "dark") {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, colorScheme });
  const page = await context.newPage();
  const t0 = Date.now();
  await page.goto(url + query);
  await page.waitForFunction(() => window.__dots?.debug().status.firstPaintMs != null, null, { timeout: 120_000 });
  const firstPaintWallMs = Date.now() - t0;
  return { page, context, firstPaintWallMs };
}

const result = { url, at: new Date().toISOString(), userAgent: null };

// --- 1. load, then pan and zoom over everything ----------------------------------------------
{
  const { page, context, firstPaintWallMs } = await open({ width: 1440, height: 900 });
  result.userAgent = await page.evaluate(() => navigator.userAgent);
  const first = (await debug(page)).status;
  const t0 = Date.now();
  await settled(page);
  const all = (await debug(page)).status;
  result.load = {
    navigationToFirstPointsMs: firstPaintWallMs,
    sceneToFirstPointsMs: Math.round(first.firstPaintMs),
    pointsInFirstFrameOrSoonAfter: first.pointsDrawn,
    firstPointsToAllTilesMs: Date.now() - t0,
    tiles: all.tilesLoaded,
    points: all.pointsDrawn,
    megabytes: +(all.bytesLoaded / 2 ** 20).toFixed(1),
  };
  await shot(page, "overview-script-dark.png");

  result.panZoom = {};
  for (const [label, column] of [["colour by script", "0"], ["density only", "-1"]]) {
    await page.getByLabel("Colour by").selectOption(column);
    await page.waitForTimeout(500);
    result.panZoom[label] = await page.evaluate(() => window.__dots.bench(10_000));
    if (column === "-1") await shot(page, "overview-density-dark.png");
  }
  await page.getByLabel("Colour by").selectOption("0");

  // --- 2. lasso selections, from a few thousand points to most of the dataset ---------------
  await page.getByRole("button", { name: "Find" }).waitFor({ state: "visible" });
  await page.waitForFunction(() => !document.querySelector('input[type="search"]').disabled, null, { timeout: 180_000 });
  result.lasso = [];
  for (const fraction of [0.03, 0.08, 0.2, 0.35, 0.49]) {
    const run = await page.evaluate(async (fraction) => {
      const scene = window.__dots;
      const v = scene.debug().view;
      const cx = (v.x0 + v.x1) / 2, cy = (v.y0 + v.y1) / 2, r = Math.min(v.x1 - v.x0, v.y1 - v.y0) * fraction;
      const polygon = [];
      for (let i = 0; i < 96; i++) polygon.push(cx + r * Math.cos((i / 96) * 2 * Math.PI) * (1 + 0.25 * Math.sin(i)), cy + r * Math.sin((i / 96) * 2 * Math.PI));
      scene.clearSelection(); // empties the panel, so the token statistics waited for below are this selection's
      await new Promise((r) => setTimeout(r, 100));
      scene.selectPolygon(polygon);
      const t0 = performance.now();
      while (!scene.debug().selection || scene.debug().selection.live) await new Promise((r) => setTimeout(r, 1));
      return { ...scene.debug().selection, vertices: 96, waitedMs: performance.now() - t0 };
    }, fraction);
    // The token statistics from DuckDB-WASM report their own time in the panel.
    let duckdbMs = null;
    const t0 = Date.now();
    if (run.n > 0) {
      const duck = page.getByText(/DuckDB-WASM, \d+ ms/);
      await duck.waitFor({ timeout: 60_000 });
      duckdbMs = Number((await duck.innerText()).match(/(\d+) ms/)[1]);
    }
    result.lasso.push({ radiusShareOfView: fraction, ...run, duckdbMs, selectionToTokensWallMs: Math.round(run.waitedMs + (Date.now() - t0)) });
    if (fraction === 0.2) await shot(page, "lasso-selection-dark.png");
  }

  // --- 3. a real mouse drag: statistics while the button is still down ---------------------
  await page.evaluate(() => window.__dots.clearSelection());
  await page.getByRole("button", { name: "Lasso" }).click();
  const box = await page.getByTestId("plot").boundingBox();
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2, r = Math.min(box.width, box.height) * 0.3;
  const live = [];
  await page.mouse.move(cx + r, cy);
  await page.mouse.down();
  for (let i = 1; i < 60; i++) {
    await page.mouse.move(cx + r * Math.cos((i / 60) * 2 * Math.PI), cy + r * Math.sin((i / 60) * 2 * Math.PI));
    const s = (await debug(page)).selection;
    if (s?.live) live.push({ n: s.n, latencyMs: s.latencyMs, workerMs: s.workerMs });
  }
  const beforeRelease = live.at(-1) ?? null;
  await page.mouse.up();
  live.sort((a, b) => a.latencyMs - b.latencyMs);
  result.liveDrag = {
    updatesObservedWhileDragging: live.length,
    countShownBeforeRelease: beforeRelease?.n ?? null,
    latencyMs: live.length ? { median: live[live.length >> 1].latencyMs, max: live.at(-1).latencyMs } : null,
  };
  await context.close();
}

// --- 4. bounded memory: a quarter of the dataset as the budget, then roam -------------------
{
  const budget = 300_000;
  const { page, context } = await open({ width: 1440, height: 900 }, `?budget=${budget}`);
  await settled(page);
  const samples = await page.evaluate(async () => {
    const scene = window.__dots;
    const home = scene.debug().view;
    const out = [];
    let seed = 7;
    const rnd = () => (seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31;
    for (let step = 0; step < 40; step++) {
      scene.resetView();
      scene.flyTo(home.x0 + rnd() * (home.x1 - home.x0), home.y0 + rnd() * (home.y1 - home.y0));
      scene.zoom(2 ** Math.floor(rnd() * 5));
      const t0 = performance.now();
      for (;;) {
        await new Promise((r) => setTimeout(r, 50));
        const s = scene.debug().status;
        if ((s.tilesLoaded + s.failed.length === s.tilesWanted && s.pointsDrawn > 0) || performance.now() - t0 > 8000) break;
      }
      const s = scene.debug().status;
      out.push({ held: s.pointsHeld, drawn: s.pointsDrawn, heapMb: performance.memory ? performance.memory.usedJSHeapSize / 2 ** 20 : null });
    }
    return out;
  });
  result.boundedMemory = {
    budgetPoints: budget,
    capPoints: budget * 1.25,
    views: samples.length,
    maxPointsHeld: Math.max(...samples.map((s) => s.held)),
    maxPointsDrawn: Math.max(...samples.map((s) => s.drawn)),
    jsHeapMb: samples[0].heapMb == null ? null : { first: +samples[0].heapMb.toFixed(1), last: +samples.at(-1).heapMb.toFixed(1), max: +Math.max(...samples.map((s) => s.heapMb)).toFixed(1) },
  };
  await context.close();
}

// --- 5. screenshots in the light theme and at phone width -----------------------------------
if (shots) {
  for (const [name, viewport, scheme] of [["overview-script-light.png", { width: 1440, height: 900 }, "light"], ["phone-dark.png", { width: 412, height: 915 }, "dark"]]) {
    const { page, context } = await open(viewport, "", scheme);
    await settled(page);
    await page.getByRole("button", { name: "Select view" }).click();
    await page.waitForTimeout(1500);
    await shot(page, name);
    await context.close();
  }
}

await browser.close();
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, JSON.stringify(result, null, 1) + "\n");
console.log(JSON.stringify(result, null, 1));
