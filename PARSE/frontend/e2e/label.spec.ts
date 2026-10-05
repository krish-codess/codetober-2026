import { expect, test } from "@playwright/test";

// Primary journey, against a live stack, at phone width, using only the keyboard:
// sign in -> first queue item appears with model suggestions -> adjust -> save -> next item ->
// the label is reflected in the taxonomy counts.
test("an annotator labels an item with the keyboard and the count moves", async ({ page }) => {
  const token = process.env.E2E_TOKEN ?? "change-me-annotator-token";

  await page.goto("/");
  await page.getByLabel("Access token").fill("definitely-wrong");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("alert")).toContainText("not recognised");

  await page.getByLabel("Access token").fill(token);
  await page.keyboard.press("Enter");

  const heading = page.locator("h2.feedback");
  await expect(heading).toBeVisible();
  await expect(heading).toBeFocused(); // focus lands on the thing to read
  const firstText = await heading.textContent();
  await expect(page.locator(".suggestion").first()).toBeVisible();
  await expect(page.getByText(/Model confidence \d+%/)).toBeVisible();

  // nothing on the page scrolls sideways at 390px
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  const first = page.locator(".suggestion input").first();
  const before = await first.isChecked();
  await page.keyboard.press("1");
  expect(await first.isChecked()).toBe(!before);
  if (before) await page.keyboard.press("1"); // leave at least the top suggestion selected
  await expect(first).toBeChecked();

  await page.keyboard.press("Enter");
  await expect(heading).not.toHaveText(firstText ?? "");
  await expect(heading).toBeFocused();
  await expect(page.getByTestId("progress")).toContainText("1 labelled this session");

  await page.keyboard.press("s"); // skip works and does not count as labelled
  await expect(page.getByTestId("progress")).toContainText("1 labelled this session");

  await page.getByRole("button", { name: "Performance" }).click();
  await expect(page.getByRole("heading", { name: /Model v\d+ on held-out data/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "By language" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "By taxonomy node" })).toBeVisible();

  await page.getByRole("button", { name: "Taxonomy" }).click();
  await expect(page.getByRole("heading", { name: /Taxonomy v\d+/ })).toBeVisible();
  // an annotator must not be offered admin controls
  await expect(page.getByRole("button", { name: "Retrain now" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Change the taxonomy" })).toHaveCount(0);
});
