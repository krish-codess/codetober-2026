/**
 * Contrast is checked, not eyeballed: parse the tokens from index.css for the light and the dark theme
 * and assert WCAG 2.1 ratios — 4.5:1 for text, 3:1 for graphical objects (chart lines, glyphs, focus).
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const css = readFileSync(resolve(__dirname, 'index.css'), 'utf8')

function tokens(block: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const m of block.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})/g)) out[m[1]] = m[2]
  return out
}

const light = tokens(css.slice(css.indexOf(':root {'), css.indexOf('@media (prefers-color-scheme: dark)')))
const dark = { ...light, ...tokens(css.slice(css.indexOf('@media (prefers-color-scheme: dark)'), css.indexOf('* {'))) }

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) =>
    c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
  )
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const TEXT_PAIRS: [string, string][] = [
  ['text', 'bg'], ['text', 'surface'], ['muted', 'surface'], ['muted', 'bg'], ['accent-text', 'accent'],
  ['warn-text', 'warn-bg'], ['err-text', 'err-bg'], ['s1', 'surface'],
  ['text', 'defl2'], ['text', 'defl1'], ['text', 'flat'], ['text', 'infl1'], ['text', 'infl2'],
]
const GRAPHIC_PAIRS: [string, string][] = [
  ['s0', 'surface'], ['s1', 'surface'], ['s2', 'surface'], ['s3', 'surface'], ['s4', 'surface'],
  ['up', 'surface'], ['down', 'surface'], ['focus', 'bg'], ['focus', 'surface'], ['accent', 'surface'],
]

describe.each([
  ['light', light],
  ['dark', dark],
])('%s theme', (_name, t) => {
  it.each(TEXT_PAIRS)('text %s on %s meets 4.5:1', (fg, bg) => {
    expect(contrast(t[fg], t[bg])).toBeGreaterThanOrEqual(4.5)
  })
  it.each(GRAPHIC_PAIRS)('graphic %s on %s meets 3:1', (fg, bg) => {
    expect(contrast(t[fg], t[bg])).toBeGreaterThanOrEqual(3)
  })
})
