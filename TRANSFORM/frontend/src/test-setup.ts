import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'
import { clearCache } from './lib/api'

afterEach(() => {
  cleanup()
  clearCache()
})
