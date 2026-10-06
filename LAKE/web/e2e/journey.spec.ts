// The primary journey: open the published results, read the tradeoff chart, inspect a variant,
// describe a workload, and follow the recommendation back to its measurements.
import { expect, test } from "@playwright/test";

test("from published results to a costed recommendation", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "The Compression Bake-Off" })).toBeVisible();
  await expect(page.getByText("published run")).toBeVisible();

  // Chart: every variant with a measurement is a focusable mark.
  const marks = page.getByRole("button", { name: /query time, warm median/ });
  expect(await marks.count()).toBeGreaterThanOrEqual(20);

  // Keyboard only: reach the chart, move, select.
  await marks.first().focus();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Enter");
  const selected = page.locator('g.mark[aria-pressed="true"]');
  await expect(selected).toHaveCount(1);
  const id = (await selected.getAttribute("aria-label"))!.split(":")[0];
  await expect(page.getByRole("region", { name: `Measurements for ${id}` })).toBeVisible();

  // Recommendation: a scan-heavy serverless workload should never be told to keep CSV.
  await page.getByLabel(/Dataset size/).fill("2000");
  await page.getByLabel("Optimise for").selectOption("cost");
  const pick = page.locator(".pick .link");
  await expect(pick).toHaveText(/^(parquet|orc)-/);
  await expect(page.locator(".why li").first()).toContainText("2000 GB");
  await expect(page.getByRole("region", { name: "Every variant, priced" }).getByRole("row")).toHaveCount(7);

  // Following the pick lands focus on its detail panel.
  const picked = await pick.innerText();
  await pick.click();
  await expect(page.getByRole("heading", { level: 3, name: picked })).toBeFocused();

  // Pushdown evidence is on the page, as text, not only as colour.
  const pushdown = page.getByRole("region", { name: "Pushdown by variant" });
  await expect(pushdown.getByRole("row", { name: /^csv-none/ })).toContainText("✕ no");
  await expect(pushdown.getByRole("row", { name: /^parquet-zstd3-rg100k/ })).toContainText("✓ yes");

  // Responsive: wide tables scroll inside their own region; the page itself never scrolls sideways.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);

  await page.screenshot({ path: `../docs/screenshots/${testInfo.project.name}.png`, fullPage: true });
});

test("an invalid workload is explained and nothing is sent", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".pick .link")).toBeVisible();
  let sent = 0;
  page.on("request", (r) => { if (r.url().includes("/api/recommend")) sent += 1; });
  await page.getByLabel(/Dataset size/).fill("-5");
  await expect(page.getByText("Enter a size above 0 GB.")).toBeVisible();
  await page.waitForTimeout(500);
  expect(sent).toBe(0);
});
