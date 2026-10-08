import { defineConfig, devices } from "@playwright/test";

// End to end against the real thing: the API process serving the built UI, on a database ingested
// from the committed raw results. SQUEEZE_E2E_BASE_URL points it at a running deployment instead.
const external = process.env.SQUEEZE_E2E_BASE_URL;
const python = process.env.SQUEEZE_PYTHON ?? "python";

export default defineConfig({
  testDir: "./e2e",
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  use: { baseURL: external ?? "http://127.0.0.1:8019", trace: "retain-on-failure", channel: process.env.CI ? undefined : "chrome" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
  webServer: external
    ? undefined
    : {
        command: `${python} -m squeeze ingest && ${python} -m squeeze serve --port 8019`,
        cwd: "..",
        url: "http://127.0.0.1:8019/healthz",
        reuseExistingServer: !process.env.CI,
        env: { SQUEEZE_DB: "web/test-results/e2e.db", SQUEEZE_LOG_LEVEL: "WARNING" },
      },
});
