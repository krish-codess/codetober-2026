import { expect, test } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";

// The primary journey, in a real browser against the real API and a seeded PostGIS:
// see the fleet -> open the top-ranked dark event -> read why -> replay that vessel's track -> come back.
test("from the live map to a vessel's replayed track and back", async ({ page }, info) => {
  const shots = process.env.GHOST_SCREENSHOTS && info.project.name === "desktop" ? process.env.GHOST_SCREENSHOTS : null;
  await page.goto("/");
  await expect(page.getByText(/vessels tracked/)).toBeVisible();
  const first = page.locator("button.event").first();
  await expect(first).toBeVisible();
  await expect(first).toContainText("Went dark");
  // The map and the panel are both on screen, at phone width too.
  const map = await page.locator(".map").boundingBox();
  const viewport = page.viewportSize()!;
  expect(map!.width).toBeGreaterThan(viewport.width * 0.5);
  expect(map!.height).toBeGreaterThan(200);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  await first.focus();
  await page.keyboard.press("Enter");
  await expect(first).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".detail li").first()).toContainText("silent for");
  if (shots) await page.screenshot({ path: `${shots}/live-map.png` });

  await page.getByRole("button", { name: "Investigate this vessel's track" }).click();
  await expect(page).toHaveURL(/#\/vessel\/\d{9}$/);
  await expect(page.getByRole("heading", { level: 1 })).toBeFocused(); // focus follows the view change
  await expect(page.getByText(/positions over/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Silences on record" })).toBeVisible();

  const slider = page.getByRole("slider");
  const start = await slider.getAttribute("aria-valuetext");
  await slider.focus();
  await page.keyboard.press("End");
  await expect(slider).not.toHaveAttribute("aria-valuetext", start!);
  await expect(page.getByRole("button", { name: "↺ Replay" })).toBeVisible();
  await page.locator("section.events button.event").first().click(); // jump to the moment it vanished
  await expect(page.getByRole("button", { name: "▶ Play" })).toBeVisible();
  if (shots) await page.screenshot({ path: `${shots}/investigation.png` });

  await page.getByRole("button", { name: /Back to the live map/ }).click();
  await expect(page.getByText(/vessels tracked/)).toBeVisible();
});

test("an unknown vessel in the URL is explained", async ({ page }) => {
  await page.goto("/#/vessel/257999999");
  await expect(page.getByText("No vessel with MMSI 257999999 has been seen.")).toBeVisible();
});

// Frame-rate evidence: N synthetic vessels, every one dead-reckoned and re-uploaded each frame.
// The measured numbers are written out; the assertion is a floor that software rendering in CI can meet.
test("renders 20,000 moving vessels at interactive frame rates", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "measured once, on the desktop profile");
  await page.goto("/?bench=20000");
  await expect(page.getByText(/Benchmark mode: 20,000/)).toBeVisible();
  await page.waitForTimeout(2000); // warm-up: shader compile, first buffer upload
  await page.evaluate(() => ((window as unknown as { __frames: number[] }).__frames.length = 0));
  await page.mouse.move(400, 300);
  await page.mouse.wheel(0, -300); // interact while measuring: zoom, then pan
  await page.mouse.down();
  await page.mouse.move(600, 380, { steps: 30 });
  await page.mouse.up();
  await page.waitForTimeout(5000);
  const frames = (await page.evaluate(() => (window as unknown as { __frames: number[] }).__frames)).sort((a, b) => a - b);
  const q = (p: number) => Number(frames[Math.min(frames.length - 1, Math.floor(p * frames.length))].toFixed(1));
  const result = { vessels: 20000, frames: frames.length, ms: { p50: q(0.5), p95: q(0.95), p99: q(0.99), max: q(1) }, over_33ms: frames.filter((f) => f > 33.4).length,
    renderer: await page.evaluate(() => { const gl = document.createElement("canvas").getContext("webgl2"); const ext = gl?.getExtension("WEBGL_debug_renderer_info"); return ext ? gl!.getParameter(ext.UNMASKED_RENDERER_WEBGL) : "unknown"; }) }; // prettier-ignore
  mkdirSync("test-results", { recursive: true });
  writeFileSync("test-results/frames.json", JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
  expect(frames.length).toBeGreaterThan(60);
  expect(result.ms.p95).toBeLessThan(process.env.CI ? 200 : 50);
});
