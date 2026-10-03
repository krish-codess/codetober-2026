import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { IndexPoint, Patch, Shock } from '../lib/api'
import { IndexChart } from './IndexChart'

const pts: IndexPoint[] = [
  { day: '2026-01-01', value: 100, coverage: 1, status: 'ok', vintage: 1, revised: false },
  { day: '2026-01-02', value: 101, coverage: 0.7, status: 'partial', vintage: 1, revised: false },
  { day: '2026-01-03', value: null, coverage: 0.3, status: 'insufficient', vintage: 1, revised: false },
  { day: '2026-01-04', value: 80, coverage: 1, status: 'ok', vintage: 2, revised: true },
]
const patches: Patch[] = [
  {
    world_id: 'w', patch_id: 'p1', released_at: '2026-01-03T11:00:00Z', version: '1.1', title: 'Open Market',
    tags: ['services'], is_major: true, source: 'rss', notes_excerpt: '', impact: null,
  },
]
const shocks: Shock[] = [
  {
    day: '2026-01-04', log_change: -0.22, robust_z: -9, direction: 'down', persistence: 0.9,
    attributions: [{ patch_id: 'p1', title: 'Open Market', released_at: '2026-01-03T11:00:00Z', lag_hours: 25, relevance: 0.9, rank: 1 }],
  },
]

describe('IndexChart', () => {
  it('is keyboard operable with a live readout', async () => {
    const user = userEvent.setup()
    render(<IndexChart points={pts} patches={patches} shocks={shocks} label="Test index" />)
    const chart = screen.getByRole('application', { name: /Test index/ })
    chart.focus()
    await user.keyboard('{End}')
    const readout = screen.getByTestId('readout')
    expect(readout).toHaveTextContent('80')
    expect(readout).toHaveTextContent('revised (v2)')
    expect(readout).toHaveTextContent(/▼ shock −19\.7% — likely Open Market/)
    await user.keyboard('{ArrowLeft}')
    expect(readout).toHaveTextContent('no value')
    expect(readout).toHaveTextContent('insufficient')
    await user.keyboard('{ArrowLeft}')
    expect(readout).toHaveTextContent('partial')
    await user.keyboard('{Home}')
    expect(readout).toHaveTextContent('100')
  })

  it('never draws through a day without a value (gap, not interpolation)', () => {
    const { container } = render(<IndexChart points={pts} patches={[]} shocks={[]} label="x" />)
    const d = container.querySelector('path.series')?.getAttribute('d') ?? ''
    expect(d.match(/M/g)?.length).toBe(2) // two separate segments around the gap
  })

  it('encodes shock direction and persistence with shape, not only colour', () => {
    const { container } = render(<IndexChart points={pts} patches={patches} shocks={shocks} label="x" />)
    const shock = container.querySelector('path.shock')
    expect(shock).toHaveClass('down', 'persistent')
    expect(shock?.querySelector('title')).toHaveTextContent('Down shock')
    expect(container.querySelector('.patch.major .patch-label')).toHaveTextContent('v1.1')
  })
})
