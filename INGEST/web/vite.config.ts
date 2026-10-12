/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The UI is static files: served by the API process, or from a CDN with VITE_API_BASE pointing at the API.
const api = process.env.GHOST_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": api, "/readyz": api } },
  test: { globals: true, environment: "jsdom", setupFiles: "./src/setup.ts", include: ["src/**/*.test.{ts,tsx}"], restoreMocks: true },
});
