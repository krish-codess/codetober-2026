/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The UI is static files. In production a CDN (nginx in the compose stack) serves them and
// forwards /v1 and /s to the API; in dev and preview Vite does the forwarding.
const api = process.env.WRAPPED_API_URL ?? "http://127.0.0.1:8000";
const proxy = { "/v1": api, "/s": api };

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  preview: { proxy },
  test: { globals: true, environment: "jsdom", setupFiles: "./src/setup.ts", include: ["src/**/*.test.tsx"], restoreMocks: true },
});
