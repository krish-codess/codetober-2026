/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev: the app calls /api/* on its own origin; Vite forwards to the API (nginx does the same in prod).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': { target: process.env.API_URL ?? 'http://localhost:8000', rewrite: (p) => p.replace(/^\/api/, '') } },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
