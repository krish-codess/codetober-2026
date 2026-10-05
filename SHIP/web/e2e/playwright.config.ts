import { defineConfig } from '@playwright/test'

// Runs against the compose stack (docker compose up -d), freshly created so the schema is at version 01.
// Uses the Chrome already on the machine; set PW_CHANNEL=msedge to use Edge instead.
export default defineConfig({
  testDir: '.',
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.WEB_URL ?? 'http://127.0.0.1:8089',
    channel: process.env.PW_CHANNEL ?? 'chrome',
    viewport: { width: 1280, height: 900 },
    trace: 'retain-on-failure',
  },
})
