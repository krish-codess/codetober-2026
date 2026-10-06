import { defineConfig, devices } from "@playwright/test";

// End to end against the real API serving the real built UI and the committed, published results.
const python = process.env.BAKEOFF_PYTHON ?? "python";
export default defineConfig({
  testDir: "./e2e",
  retries: process.env.CI ? 1 : 0,
  use: { baseURL: "http://127.0.0.1:8017" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
  webServer: {
    command: `${python} -m bakeoff serve`,
    cwd: "..",
    url: "http://127.0.0.1:8017/healthz",
    reuseExistingServer: !process.env.CI,
    env: { LAKE_PORT: "8017", LAKE_DATA_DIR: "./.e2e-no-local-run", LAKE_PUBLISHED_DIR: "./results", LAKE_LOG_LEVEL: "WARNING" },
  },
});
