// Capture the screenshots used in the README from a running stack.
//   PW_CHANNEL=msedge node e2e/screenshots.mjs        (tokens from the environment, see .env.example)
import { chromium } from "@playwright/test";

const base = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8141";
const out = new URL("../../docs/screenshots/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const browser = await chromium.launch({ channel: process.env.PW_CHANNEL });

async function session(token, viewport, colorScheme = "light") {
  const context = await browser.newContext({ viewport, colorScheme, deviceScaleFactor: 2 });
  const page = await context.newPage();
  await page.goto(base);
  await page.getByLabel("Access token").fill(token);
  await page.keyboard.press("Enter");
  return page;
}

// 1. labelling at phone width
let page = await session(process.env.ANNOTATOR_TOKEN ?? "change-me-annotator-token", { width: 390, height: 844 });
await page.locator("h2.feedback").waitFor();
await page.screenshot({ path: `${out}label-phone.png` });

// 2. labelling on a desktop, dark, with the taxonomy search in use
page = await session(process.env.ANNOTATOR_TOKEN ?? "change-me-annotator-token", { width: 1100, height: 800 }, "dark");
await page.locator("h2.feedback").waitFor();
await page.getByLabel("Add another category").fill("deliv");
await page.screenshot({ path: `${out}label-desktop-dark.png` });

// 3. performance: summary, efficiency curve, languages
page = await session(process.env.ADMIN_TOKEN ?? "change-me-admin-token", { width: 1100, height: 900 });
await page.getByRole("button", { name: "Performance" }).click();
await page.getByRole("heading", { name: "By taxonomy node" }).waitFor();
await page.locator("figure.chart svg").waitFor();
await page.screenshot({ path: `${out}performance.png`, fullPage: false });
await page.getByRole("heading", { name: "Confidence and automatic routing" }).scrollIntoViewIfNeeded();
await page.screenshot({ path: `${out}performance-routing.png` });
await page.getByRole("checkbox", { name: /Weak branches only/ }).check();
await page.getByRole("heading", { name: "By taxonomy node" }).scrollIntoViewIfNeeded();
await page.screenshot({ path: `${out}performance-weak-nodes.png` });

// 4. taxonomy admin
await page.getByRole("button", { name: "Taxonomy" }).click();
await page.getByRole("heading", { name: /Taxonomy v\d+/ }).waitFor();
await page.screenshot({ path: `${out}taxonomy-admin.png` });

await browser.close();
console.log("screenshots written to", out);
