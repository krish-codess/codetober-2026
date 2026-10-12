import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

// The primary journey, on a desktop and on a phone: sign in, compare the models, find who the treatment hurts,
// build a budgeted policy, export it. Nothing is mocked.

const token = process.env.UPLIFT_E2E_TOKEN ?? "dev-only-token-change-me";
const shots = process.env.UPLIFT_SCREENSHOTS; // a directory; set it to refresh docs/screenshots

test("from sign-in to an exported policy", async ({ page }, testInfo) => {
  const shot = async (name: string) => {
    if (shots) await page.screenshot({ path: `${shots}/${testInfo.project.name}-${name}.png`, fullPage: true });
  };
  const noSidewaysScroll = async () =>
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);

  await page.goto("/");
  await expect(page.getByLabel("Access token")).toBeFocused();
  await page.getByLabel("Access token").fill("definitely-wrong");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("alert")).toContainText("not accepted");
  await page.getByLabel("Access token").fill(token);
  await page.keyboard.press("Enter");

  // ---- compare models on the dataset with known effects
  await page.getByRole("combobox", { name: "Dataset" }).selectOption("synthetic");
  await expect(page.getByText("ranks customers best on held-out data")).toBeVisible();
  const chart = page.getByRole("img", { name: /Qini curve for 6 models/ });
  await expect(chart).toBeVisible();
  await chart.focus();
  await page.keyboard.press("End");
  await expect(page.getByText("Targeting the top 100%")).toBeVisible();
  await page.keyboard.press("Home");
  for (let i = 0; i < 20; i++) await page.keyboard.press("ArrowRight");
  await expect(page.getByText("Targeting the top 20%")).toBeVisible();
  // at 20% targeted, an uplift model leads and the likely-buyers baseline does not
  const ranked = page.locator(".readout li");
  await expect(ranked.first()).toContainText(/learner|forest/);
  await expect(page.getByRole("row", { name: /Likely buyers/ })).toBeVisible();
  await noSidewaysScroll();
  await shot("1-compare");

  // ---- who it helps and hurts: keyboard only
  await page.getByRole("button", { name: "Who it helps and hurts" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 2, name: "Who it helps and hurts" })).toBeFocused();
  await expect(page.getByRole("img", { name: /Observed uplift by decile/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Predicted to be harmed" })).toBeVisible();
  const harmed = page.getByRole("row", { name: /history = \$350 and over AND channel = Phone/ });
  await expect(harmed).toContainText("▼ hurts");
  await noSidewaysScroll();
  await shot("2-segments");

  // ---- policy
  await page.getByRole("button", { name: "Targeting policy" }).click();
  await page.getByLabel(/^Budget/).fill("3000");
  await page.getByRole("button", { name: "Build policy" }).click();
  const result = page.getByRole("heading", { name: /^Treat 3,000 of/ });
  await expect(page.getByText(/estimating the policy.s value from the experiment/)).toBeVisible(); // says what it is doing meanwhile
  await expect(result).toBeFocused({ timeout: 30_000 }); // about 1.6 s on an idle machine; two browsers share this one
  await expect(page.getByText("▲ reliably better").first()).toBeVisible();
  await expect(page.getByText(/captures \d+% of the effect of treating everyone/)).toBeVisible();
  await noSidewaysScroll();
  await shot("3-policy");

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download activation file (CSV)" }).click();
  const file = await (await download).path();
  const lines = readFileSync(file, "utf8").trim().split(/\r?\n/); // RFC 4180 line endings
  expect(lines[0]).toBe("unit_id,predicted_uplift");
  expect(lines.length - 1).toBe(3000); // exactly what the budget pays for
  await expect(page.getByText("Downloaded 3,000 customer ids.")).toBeVisible();

  // ---- the real experiment is there too, and its honest answer about harm is "none found"
  await page.getByRole("combobox", { name: "Dataset" }).selectOption("hillstrom");
  await page.getByRole("button", { name: "Who it helps and hurts" }).click();
  await expect(page.getByText("no segment with statistically reliable harm")).toBeVisible();
  await shot("4-hillstrom-no-harm");
});
