/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  // `npm run dev` talks to the controller published by docker compose.
  server: { proxy: { '/api': { target: 'http://127.0.0.1:8088', rewrite: (p) => p.replace(/^\/api/, '') } } },
  test: { environment: 'jsdom', globals: true, include: ['src/**/*.test.tsx'], restoreMocks: true },
})
