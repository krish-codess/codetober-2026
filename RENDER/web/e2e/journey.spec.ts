import { expect, test, type Page } from "@playwright/test";

// The primary journey: open the page, see points, select some, read their statistics, filter.

async function ready(page: Page) {
  await page.goto("./");
  await expect(page.getByRole("heading", { name: "A Million Dots" })).toBeVisible();
  // Every tile the opening view wants has arrived and been drawn.
  await expect
    .poll(
      async () =>
        page.evaluate(() => {
          const s = (window as any).__dots?.debug().status;
          return !!s && s.pointsDrawn > 0 && s.tilesLoaded === s.tilesWanted;
        }),
      { timeout: 60_000 },
    )
    .toBe(true);
}

const selected = async (page: Page): Promise<number> => Number((await page.locator(".headline strong").innerText()).replace(/,/g, ""));

test("points are drawn, and a lasso returns statistics without any request", async ({ page, isMobile }) => {
  test.skip(isMobile, "the lasso is a mouse gesture; the phone test covers touch-sized controls");
  await ready(page);
  const plot = page.getByTestId("plot");
  expect((await plot.screenshot()).length, "a blank canvas compresses to almost nothing").toBeGreaterThan(8_000);

  // Wait for DuckDB-WASM to hold the metadata table, then watch the network.
  await expect(page.getByRole("button", { name: "Find" })).toBeEnabled({ timeout: 60_000 });
  const requests: string[] = [];
  page.on("request", (r) => requests.push(r.url()));

  await page.getByRole("button", { name: "Lasso" }).click();
  const box = (await plot.boundingBox())!;
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2, r = Math.min(box.width, box.height) / 4;
  await page.mouse.move(cx + r, cy);
  await page.mouse.down();
  for (let i = 1; i <= 8; i++) await page.mouse.move(cx + r * Math.cos((i * Math.PI) / 8), cy + r * Math.sin((i * Math.PI) / 8));
  // Half-way round, with the button still down, the count is already there.
  await expect(page.getByText("updating")).toBeVisible();
  expect(await selected(page)).toBeGreaterThan(0);
  for (let i = 9; i <= 15; i++) await page.mouse.move(cx + r * Math.cos((i * Math.PI) / 8), cy + r * Math.sin((i * Math.PI) / 8));
  await page.mouse.up();

  await expect(page.getByText("Most frequent:")).toBeVisible();
  const n = await selected(page);
  expect(n).toBeGreaterThan(0);
  const info = await page.evaluate(() => (window as any).__dots.debug().selection);
  expect(info.exact).toBe(true);
  expect(info.latencyMs).toBeLessThan(1000);
  expect(requests, "selection and its statistics must not touch the network").toEqual([]);

  // Filtering by metadata narrows the same selection, still locally.
  await page.locator(".legend input[type=checkbox]").first().uncheck();
  await expect.poll(() => selected(page)).toBeLessThan(n);
  expect(requests).toEqual([]);

  await page.getByRole("button", { name: "Clear" }).click();
  await expect(page.getByText("Nothing selected")).toBeVisible();
});

test("the plot is operable from the keyboard", async ({ page, isMobile }) => {
  test.skip(isMobile, "no keyboard on a phone");
  await ready(page);
  const plot = page.getByRole("application");
  await plot.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".headline")).toContainText("points");
  const all = await selected(page);
  await page.keyboard.press("Escape");
  await expect(page.getByText("Nothing selected")).toBeVisible();
  await page.keyboard.press("+");
  await page.keyboard.press("+");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Enter");
  await expect.poll(() => selected(page)).toBeLessThan(all);
  await page.keyboard.press("0");
});

test("phone width: no sideways scroll, controls reachable, selection works by tap", async ({ page, isMobile }) => {
  test.skip(!isMobile, "desktop is covered above");
  await ready(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Select view" }).tap();
  await expect(page.locator(".headline")).toContainText("points");
  expect(await selected(page)).toBeGreaterThan(0);
  const size = await page.getByRole("button", { name: "Clear" }).boundingBox();
  expect(size!.height).toBeGreaterThanOrEqual(40);
});

test("a tile that keeps failing is reported, not hidden, and can be retried", async ({ page, isMobile }) => {
  test.skip(isMobile, "same code path on every device");
  let blocked = 0;
  await page.route(/\/1-0-0\.bin$/, (route) => {
    blocked++;
    return route.fulfill({ status: 503, body: "down" });
  });
  await page.goto("./");
  await expect(page.getByText(/could not be loaded/)).toBeVisible({ timeout: 60_000 });
  expect(blocked, "four attempts with backoff, then it gives up").toBe(4);
  await page.unroute(/\/1-0-0\.bin$/);
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText(/could not be loaded/)).toBeHidden({ timeout: 30_000 });
});
