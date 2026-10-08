import { expect, test } from "@playwright/test";

// The primary journey on real data: open the explorer, find the frontier on the measured target,
// inspect a compressed model and why it passed, see which layers were kept in float, then fetch
// the package that would go on a board.
test("explore the measured tradeoff, inspect a model, download its package", async ({ page, request }, info) => {
  const shot = (name: string) => page.screenshot({ path: `../docs/screenshots/${name}-${info.project.name}.png`, fullPage: true });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /Accuracy against cost on Laptop/ })).toBeVisible();

  // real measurements are on the chart, and the page does not scroll sideways at this width
  const marks = page.getByRole("button", { name: /top-1, .* ms/ });
  expect(await marks.count()).toBeGreaterThanOrEqual(4);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await shot("explorer");

  // the naive INT8 student is in the table as a failure, not hidden
  await expect(page.getByRole("row", { name: /^student-kd-int8\b/ })).toContainText("✕ fail");

  // keyboard: focus a mark, open it, land on its detail
  const mixed = page.getByRole("button", { name: /^student-kd-int8-mixed:/ });
  await mixed.focus();
  await page.keyboard.press("Enter");
  const heading = page.getByRole("heading", { name: "student-kd-int8-mixed", exact: true });
  await expect(heading).toBeFocused();
  const detail = page.locator("section", { has: heading });
  await expect(detail).toContainText("Passed the accuracy gate");
  await expect(detail.getByRole("listitem")).toHaveText([/^teacher-r50/, /^student-kd/, /^student-kd-int8-mixed/]);
  await expect(page.getByText("kept float32").first()).toBeVisible();
  await shot("detail");

  // an unmeasured target says so instead of showing an empty chart
  await page.getByLabel("Hardware target").selectOption("rpi5");
  await expect(page.getByText(/Nothing has been measured on Raspberry Pi 5/)).toBeVisible();
  await expect(page.getByRole("row", { name: /^teacher-r50\b/ })).toContainText("not measured on this target");

  // packages: the hosted one downloads and is the archive its id says it is
  await page.getByRole("link", { name: "Packages" }).click();
  const ci = page.locator("section", { has: page.getByRole("heading", { name: /GitHub-hosted runner/ }) });
  const href = await ci.getByRole("link", { name: "Download package" }).getAttribute("href");
  const archive = await request.get(href!);
  expect(archive.ok()).toBe(true);
  const digest = Buffer.from(await crypto.subtle.digest("SHA-256", await archive.body())).toString("hex");
  expect(href).toContain(digest);
  await expect(page.locator("section", { has: page.getByRole("heading", { name: "Raspberry Pi 5" }) })).toContainText("Not hosted on this server");
  await shot("packages");
});
