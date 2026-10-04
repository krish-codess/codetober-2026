import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': process.env.API_PROXY ?? 'http://localhost:8080' } },
  // threads: the forks pool times out starting workers on slow Windows disks
  test: { pool: 'threads', environment: 'jsdom', setupFiles: './src/test-setup.ts', include: ['src/**/*.test.{ts,tsx}'] },
})
