import { expect, test, type Page } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'

// The primary journey in a real browser against the real stack: an operator changes a column's type on a
// table that application v1 is writing to, and cannot drop the old column until v1 is gone.
// Set SCREENSHOTS=1 to refresh docs/screenshots.

const root = new URL('../../', import.meta.url)
const env = Object.fromEntries(
  ['.env.example', '.env'].flatMap((f) => {
    try {
      return readFileSync(new URL(f, root), 'utf8').split(/\r?\n/).filter((l) => /^[A-Z_]+=/.test(l)).map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)])
    } catch {
      return []
    }
  }),
)
const desired = readFileSync(new URL('desired/02_amount_to_cents.json', root), 'utf8')
const compose = (...args: string[]) => execFileSync('docker', ['compose', ...args], { cwd: root, stdio: 'pipe' })

async function shot(page: Page, name: string) {
  if (process.env.SCREENSHOTS) await page.screenshot({ path: new URL(`docs/screenshots/${name}.png`, root).pathname.replace(/^\/([A-Za-z]:)/, '$1'), fullPage: true })
}

test('change a column type under live load, from the console', async ({ page }) => {
  test.setTimeout(20 * 60_000)
  const long = { timeout: 15 * 60_000 }

  await page.goto('/')
  await page.getByLabel('API token').fill('not-the-token')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('alert')).toContainText('not accepted')
  await page.getByLabel('API token').fill(env.SHIPD_OPERATOR_TOKEN!)
  await page.getByLabel('API token').press('Enter') // forms submit from the keyboard
  await expect(page.getByRole('navigation', { name: 'Views' })).toBeVisible()
  await expect(page.getByRole('button', { name: /01_initial/ })).toBeVisible()

  // Plan from the desired schema, read the locks it will take, start it.
  await page.getByLabel(/Desired schema/).fill(desired)
  await page.getByRole('button', { name: 'Show the plan' }).click()
  await expect(page.getByRole('cell', { name: /touch all ~[\d,]+ rows/ })).toBeVisible()
  await expect(page.getByRole('cell', { name: 'ACCESS EXCLUSIVE' }).first()).toBeVisible()
  await shot(page, '1-plan')
  await page.getByRole('button', { name: 'Start this migration' }).click()

  // Real progress while v1 keeps writing.
  await expect(page.getByRole('heading', { name: /02_amount_to_cents/ })).toBeVisible()
  await expect(page.getByText(/Backfill: [\d,]+ of about [\d,]+ rows/)).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText(/about \d+ (s|min) left/)).toBeVisible({ timeout: 60_000 })
  await shot(page, '2-backfill')
  await expect(page.getByRole('heading', { name: /Expanded: both versions live/ })).toBeVisible(long)
  await expect(page.getByText('Old and new agree')).toBeVisible(long)
  await shot(page, '3-expanded-verified')

  // The gate: v1 is connected, so the old column stays.
  await page.getByRole('button', { name: 'Complete (drop old column)' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: 'Keep it as it is' })).toBeFocused() // the safe choice has focus
  await dialog.getByRole('button', { name: 'Complete' }).click()
  await expect(page.getByRole('alert').filter({ hasText: 'Refused' })).toContainText('shop v1 on 01_initial')
  await shot(page, '4-contract-refused')

  await page.getByRole('link', { name: 'Compatibility' }).click()
  const v1 = page.getByRole('row').filter({ has: page.getByRole('rowheader', { name: 'v1', exact: true }) })
  await expect(v1.getByRole('cell').first()).toContainText(/[1-9]\d* connections?/)
  await shot(page, '5-compatibility')
  await page.getByLabel(/May this application version/).fill('v2')
  await page.getByRole('button', { name: 'Check' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'v2' })).toContainText('Yes')

  compose('stop', 'shop-v1') // the old application version is retired
  await expect(v1.getByRole('cell').first()).toContainText('0 connections', { timeout: 30_000 })

  await page.getByRole('link', { name: 'Migrations' }).click()
  await page.getByRole('button', { name: 'Complete (drop old column)' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Complete' }).click()
  await expect(page.getByRole('heading', { name: /02_amount_to_cents.*Completed/ })).toBeVisible(long)
  await expect(page.getByRole('button', { name: 'Complete (drop old column)' })).toBeDisabled()
  await shot(page, '6-completed')

  await page.getByRole('link', { name: 'Analytics' }).click()
  await expect(page.getByRole('rowheader', { name: /02_amount_to_cents/ }).first()).toBeVisible()
  await expect(page.getByText(/rows per second/)).toBeVisible()
  await shot(page, '7-analytics')

  // Keyboard only: reach another view and come back without a pointer.
  await page.getByRole('link', { name: 'Migrations' }).focus()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: 'Compatibility' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Compatibility matrix' })).toBeVisible()

  // Phone width: nothing spills sideways, in either theme.
  await page.setViewportSize({ width: 390, height: 844 })
  for (const view of ['Migrations', 'Compatibility', 'Analytics']) {
    await page.getByRole('link', { name: view }).click()
    await page.waitForTimeout(500)
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), `${view} overflows at 390px`).toBeLessThanOrEqual(0)
  }
  await page.getByRole('link', { name: 'Migrations' }).click()
  await shot(page, '8-phone')
  await page.emulateMedia({ colorScheme: 'dark' })
  await shot(page, '9-phone-dark')
})
