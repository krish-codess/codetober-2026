import { expect, test } from "@playwright/test";

// The primary journey: open a personal link, watch the story, share a card, and follow the share
// link as a stranger would. Runs at desktop and phone sizes (see playwright.config.ts).

const token = process.env.WRAPPED_E2E_TOKEN;
const shots = process.env.WRAPPED_SCREENSHOT_DIR; // set to refresh the screenshots in docs/

test.beforeEach(() => test.skip(!token, "set WRAPPED_E2E_TOKEN to a personal link token"));

test("open a personal link, go through the story, share a card", async ({ page, context }, testInfo) => {
  const shot = async (name: string) => {
    if (shots) await page.screenshot({ path: `${shots}/${testInfo.project.name}-${name}.png` });
  };
  const failed: string[] = [];
  page.on("pageerror", (e) => failed.push(String(e)));
  page.on("response", (r) => r.status() >= 500 && failed.push(`${r.status()} ${r.url()}`));

  await page.goto(`/#t=${token}`);
  const title = page.getByRole("heading", { level: 2 });
  await expect(title).toHaveText(/^Your \d{4}$/);
  expect(page.url(), "the token must not stay in the address bar").not.toContain(token!);
  await expect(page.getByRole("button", { name: "Previous" })).toBeDisabled();

  // Nothing may overflow sideways at this viewport.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  await page.getByRole("button", { name: "Pause" }).click();
  const count = page.locator(".count");
  const total = Number((await count.innerText()).split("/")[1]);
  expect(total).toBeGreaterThanOrEqual(4);
  await page.waitForTimeout(1800);
  await shot("1-intro");

  // Keyboard: every card is reachable, focus lands on its heading, and the value settles on a real one.
  const seen = new Set<string>();
  for (let i = 2; i <= total; i++) {
    const previous = await title.innerText();
    await page.keyboard.press("ArrowRight");
    await expect(count).toHaveText(`${i} / ${total}`);
    await expect(title).not.toHaveText(previous); // the old card leaves before the new one arrives
    await expect(title).toBeFocused();
    const text = await title.innerText();
    expect(seen.has(text), `card "${text}" appeared twice`).toBe(false);
    seen.add(text);
    if (i === 2) {
      await page.waitForTimeout(2600);
      await shot("2-card");
    }
  }
  await expect(page.getByRole("button", { name: "Next" })).toBeDisabled();
  await page.waitForTimeout(2200);
  await shot("3-summary");

  // Share the summary: a real share is created and a real PNG comes back from the server.
  await page.getByRole("button", { name: "Share" }).click();
  const sheet = page.getByRole("dialog", { name: "Share this card" });
  const image = sheet.getByRole("img");
  await expect(sheet.getByRole("button", { name: "Copy link" })).toBeVisible();
  await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(1200);
  expect(await image.evaluate((el: HTMLImageElement) => el.naturalHeight)).toBe(630);
  await shot("4-share");
  const shareUrl = await sheet.locator(".share-url a").getAttribute("href");
  expect(shareUrl).toMatch(/\/s\/[A-Za-z0-9_-]{22,}$/);

  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await expect(page.getByRole("button", { name: "Share" })).toBeFocused(); // focus returns to where it was

  // A stranger, with no token and no storage, follows the link: they get that one card and preview tags.
  const stranger = await context.browser()!.newContext();
  const landing = await stranger.newPage();
  const response = await landing.goto(new URL(shareUrl!).pathname);
  expect(response!.status()).toBe(200);
  await expect(landing.locator('meta[property="og:image"]')).toHaveAttribute("content", /card\.png\?v=/);
  await expect(landing.locator('meta[name="twitter:card"]')).toHaveAttribute("content", "summary_large_image");
  await expect.poll(() => landing.getByRole("img").evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(1200);
  if (shots) await landing.screenshot({ path: `${shots}/${testInfo.project.name}-5-shared.png` });
  const denied = await landing.request.get("/v1/wrapped");
  expect(denied.status(), "a share link is not a login").toBe(401);
  await stranger.close();

  expect(failed).toEqual([]);
});

test("a reload shows the saved story; a bad link explains itself", async ({ page }) => {
  await page.goto(`/#t=${token}`);
  await expect(page.getByRole("heading", { level: 2 })).toHaveText(/^Your \d{4}$/);
  await page.route("**/v1/wrapped", (route) => route.abort()); // the server goes away
  await page.reload();
  await expect(page.getByRole("heading", { level: 2 })).toHaveText(/^Your \d{4}$/);

  await page.unroute("**/v1/wrapped");
  await page.evaluate(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
  // Someone else's (here: invalid) link on the same device must not be answered from the browser's cache.
  await page.goto("/#t=not.a-real-token");
  await expect(page.getByRole("heading", { name: "This link no longer works" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "This link no longer works" })).toBeVisible();
});
