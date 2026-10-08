/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The UI is static files. In production the API process serves them; in dev Vite forwards /v1.
const api = process.env.SQUEEZE_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/v1": api } },
  test: { globals: true, environment: "jsdom", setupFiles: "./src/setup.ts", include: ["src/**/*.test.tsx"], restoreMocks: true },
});
