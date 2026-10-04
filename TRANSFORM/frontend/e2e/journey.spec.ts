import { expect, test } from '@playwright/test'

const shots = process.env.SCREENSHOTS ? '../docs/screenshots' : null

test('primary journey: index with patch shocks, then purchasing power', async ({ page }, info) => {
  await page.goto('/')
  // 1. Real EVE index renders with patch markers and a keyboard-driven readout
  const chart = page.getByRole('application', { name: /Price index/ })
  await expect(chart).toBeVisible()
  await expect(page.locator('.patch').first()).toBeAttached()
  await chart.focus()
  await page.keyboard.press('End')
  await expect(page.getByTestId('readout')).toContainText(/coverage \d+%/)
  if (shots && info.project.name === 'desktop') await page.screenshot({ path: `${shots}/01-index-eve.png`, fullPage: true })

  // 2. Simulated shard: the crash patch is attributed as the cause of the services shock
  await page.getByText('Simulated shard').click()
  await page.getByLabel('Division').selectOption('services')
  await expect(page.getByRole('application', { name: /Account & training services/ })).toBeVisible()
  const shocksTable = page.getByRole('table').filter({ hasText: 'Most likely patch' })
  await expect(shocksTable).toContainText('Open Market')
  if (shots && info.project.name === 'desktop') await page.screenshot({ path: `${shots}/02-crash-attribution.png`, fullPage: true })

  // 3. Purchasing power: an hour of ratting expressed in goods and labour-hours
  await page.getByRole('tab', { name: 'Purchasing power' }).click()
  await page.getByLabel('Activity').selectOption({ label: 'Ratting (NPC bounties)' })
  const answer = page.locator('.answer')
  await expect(answer).toContainText('one hour of')
  await expect(answer).toContainText(/purchasing power [▲▼•]/)
  await expect(page.getByRole('table').filter({ hasText: 'Labour-hours per item' })).toBeVisible()
  if (shots && info.project.name === 'desktop') await page.screenshot({ path: `${shots}/03-purchasing-power.png`, fullPage: true })

  // 4. Inflation matrix and integrity feed load
  await page.getByRole('tab', { name: 'Inflation' }).click()
  await expect(page.locator('table.heat')).toBeVisible()
  if (shots && info.project.name === 'desktop') await page.screenshot({ path: `${shots}/04-inflation.png`, fullPage: true })
  await page.getByRole('tab', { name: 'Market integrity' }).click()
  await expect(page.getByRole('table')).toContainText(/Extreme listing|Thin-market spike|Rejected/)
  if (shots && info.project.name === 'phone') await page.screenshot({ path: `${shots}/05-integrity-phone.png`, fullPage: true })

  // Responsive: nothing scrolls sideways at phone width
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
})

test('stale or failing API degrades visibly, not silently', async ({ page }) => {
  await page.route('**/api/v1/index**', (route) =>
    route.fulfill({ status: 503, contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'database_unavailable', message: 'db down', request_id: 'e2e' } }) }))
  await page.goto('/')
  await expect(page.getByRole('alert')).toContainText('temporarily unavailable')
  await page.unroute('**/api/v1/index**')
  await page.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByRole('application', { name: /Price index/ })).toBeVisible()
})
