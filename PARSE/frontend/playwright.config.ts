import { defineConfig } from "@playwright/test";

// Runs against a live stack (docker compose). Locally: PW_CHANNEL=chrome uses the installed browser.
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:8141",
    channel: process.env.PW_CHANNEL,
    viewport: { width: 390, height: 844 }, // phone width on purpose
  },
});
