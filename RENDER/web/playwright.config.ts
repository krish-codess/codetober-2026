import { defineConfig, devices } from "@playwright/test";

// End to end against the real thing: the site directory (built page + tiles from the pipeline)
// served by the reference API. Prepare it first with deploy/build.sh; DOTS_E2E_BASE_URL points
// the same tests at a deployment instead.
const external = process.env.DOTS_E2E_BASE_URL;
const python = process.env.DOTS_PYTHON ?? "python";

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts/,
  retries: process.env.CI ? 1 : 0,
  timeout: 90_000,
  use: { baseURL: external ?? "http://127.0.0.1:8025", trace: "retain-on-failure", channel: process.env.CI ? undefined : "chrome" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
  webServer: external
    ? undefined
    : {
        command: `${python} -m dots serve`,
        cwd: "..",
        url: "http://127.0.0.1:8025/readyz",
        reuseExistingServer: !process.env.CI,
        env: { DOTS_PORT: "8025", DOTS_LOG_LEVEL: "WARNING" },
      },
});
