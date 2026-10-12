import { defineConfig, devices } from "@playwright/test";

// End to end against a real stack: real API, real PostgreSQL, real trained models, real built UI.
//
// Two ways to point it at one:
//   UPLIFT_E2E_BASE_URL=http://localhost:8080   an already-running deployment (what CI does)
//   (unset)                                     start the API and `vite preview` here; needs a seeded
//                                               database and UPLIFT_* env for the API.
// Either way UPLIFT_E2E_TOKEN must be the admin token (the journey ends by exporting a policy).
const external = process.env.UPLIFT_E2E_BASE_URL;
const python = process.env.UPLIFT_PYTHON ?? "python";

export default defineConfig({
  testDir: "./e2e",
  retries: process.env.CI ? 1 : 0,
  timeout: 90_000,
  use: { baseURL: external ?? "http://127.0.0.1:4174", trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
  webServer: external
    ? undefined
    : [
        {
          command: `${python} -m uplift serve --port 8018`,
          cwd: "..",
          url: "http://127.0.0.1:8018/readyz",
          reuseExistingServer: !process.env.CI,
          timeout: 120_000,
          env: { UPLIFT_LOG_LEVEL: "WARNING" },
        },
        {
          command: "npm run preview",
          url: "http://127.0.0.1:4174",
          reuseExistingServer: !process.env.CI,
          env: { UPLIFT_API_URL: "http://127.0.0.1:8018" },
        },
      ],
});
