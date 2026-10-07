import { defineConfig, devices } from "@playwright/test";

// End to end against a real stack: real API, real PostgreSQL, real published run, real built UI.
//
// Two ways to point it at one:
//   WRAPPED_E2E_BASE_URL=http://localhost:8080   an already-running deployment (what CI does)
//   (unset)                                      start the API and `vite preview` here; needs a
//                                                published run and WRAPPED_* env for the API.
// Either way WRAPPED_E2E_TOKEN must be a personal link token for a full-tier user (`wrapped links`).
const external = process.env.WRAPPED_E2E_BASE_URL;
const python = process.env.WRAPPED_PYTHON ?? "python";

export default defineConfig({
  testDir: "./e2e",
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  use: { baseURL: external ?? "http://127.0.0.1:4173", trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
  webServer: external
    ? undefined
    : [
        {
          command: `${python} -m wrapped serve --port 8017`,
          cwd: "..",
          url: "http://127.0.0.1:8017/readyz",
          reuseExistingServer: !process.env.CI,
          env: { WRAPPED_PUBLIC_BASE_URL: "http://127.0.0.1:4173", WRAPPED_LOG_LEVEL: "WARNING" },
        },
        {
          command: "npm run preview",
          url: "http://127.0.0.1:4173",
          reuseExistingServer: !process.env.CI,
          env: { WRAPPED_API_URL: "http://127.0.0.1:8017" },
        },
      ],
});
