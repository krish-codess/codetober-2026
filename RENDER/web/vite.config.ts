/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build is plain static files with relative URLs, so it can sit at any path on a CDN next to
// its data/ directory. In dev, /data is forwarded to the reference API (python -m dots serve).
const api = process.env.DOTS_API_URL ?? "http://127.0.0.1:8015";

export default defineConfig({
  base: "./",
  plugins: [react()],
  server: { proxy: { "/data": api, "/api": api } },
  worker: { format: "es" },
  build: { target: "es2022", chunkSizeWarningLimit: 1500 },
  test: { globals: true, environment: "jsdom", setupFiles: "./src/setup.ts", include: ["src/**/*.test.{ts,tsx}"], restoreMocks: true },
});
