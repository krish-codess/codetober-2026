import { defineConfig, devices } from "@playwright/test";

// End to end against the real thing: the API process serving the built UI over a seeded PostGIS.
// GHOST_E2E_BASE_URL points it at a running deployment instead.
const external = process.env.GHOST_E2E_BASE_URL;
const python = process.env.GHOST_PYTHON ?? "python";

export default defineConfig({
  testDir: "./e2e",
  retries: process.env.CI ? 1 : 0,
  timeout: 90_000,
  use: { baseURL: external ?? "http://127.0.0.1:8019", trace: "retain-on-failure", channel: process.env.CI ? undefined : "chrome" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
  webServer: external
    ? undefined
    : {
        command: `${python} -m ghostfleet api --port 8019`,
        cwd: "..",
        url: "http://127.0.0.1:8019/healthz",
        reuseExistingServer: !process.env.CI,
        env: { GHOST_LOG_LEVEL: "WARNING" },
      },
});
