import { expect, test, type APIRequestContext } from '@playwright/test'
import { randomUUID } from 'node:crypto'

const ADMIN_KEY = process.env.ADMIN_API_KEY ?? 'change-me-admin'
// Screenshots for the README are only (re)written when asked for, so a normal run leaves the repo clean.
const shot = (name: string) => (process.env.SCREENSHOTS ? { path: `../docs/screenshots/${name}.png`, fullPage: true } : undefined)

/** Each test gets its own on-sale, so tests are independent of each other and of the seeded demo event. */
async function createEvent(request: APIRequestContext, opts: { opensInSeconds: number; holdSeconds: number; floor: number }) {
  const id = randomUUID()
  const res = await request.put(`/api/admin/events/${id}`, {
    headers: { 'X-Admin-Key': ADMIN_KEY },
    data: {
      name: 'E2E Night',
      onSaleAt: new Date(Date.now() + opts.opensInSeconds * 1000).toISOString(),
      holdSeconds: opts.holdSeconds,
      maxPerUser: 4,
      admissionRatePerSec: 50,
      inventory: [
        { ticketType: 'GA', section: 'FLOOR', priceCents: 9500, total: opts.floor },
        { ticketType: 'VIP', section: 'BOX', priceCents: 32000, total: 2 },
      ],
    },
  })
  expect(res.status()).toBe(201)
  return id
}

test('lobby -> queue -> hold -> pay', async ({ page, request }, testInfo) => {
  const eventId = await createEvent(request, { opensInSeconds: 6, holdSeconds: 120, floor: 5 })
  await page.goto(`/?event=${eventId}`)

  await expect(page.getByRole('heading', { level: 1, name: 'E2E Night' })).toBeVisible()
  await expect(page.getByRole('row', { name: /GA · FLOOR/ })).toContainText('Only 5 left')

  await page.getByRole('button', { name: 'Join the queue' }).click()
  const lobby = page.getByRole('heading', { name: 'You are in the lobby' })
  await expect(lobby).toBeVisible()
  await expect(lobby).toBeFocused() // focus followed the step change
  await expect(page.getByText('random place in line')).toBeVisible()
  if (testInfo.project.name === 'desktop') await page.screenshot(shot('1-lobby'))

  // A refresh must not cost you your place.
  await page.reload()
  await expect(page.getByRole('heading', { name: /You are in (the lobby|line)|It is your turn/ })).toBeVisible()

  await expect(page.getByRole('heading', { name: 'It is your turn' })).toBeVisible({ timeout: 20_000 })
  await page.getByLabel('Tickets').selectOption('2')
  if (testInfo.project.name === 'desktop') await page.screenshot(shot('2-your-turn'))
  await page.getByRole('button', { name: 'Hold 2 for $190.00' }).click()

  await expect(page.getByRole('heading', { name: 'Checkout' })).toBeFocused()
  await expect(page.getByRole('timer')).toHaveText(/^1:(5\d|4\d)$/)
  await expect(page.getByRole('row', { name: /GA · FLOOR/ })).toContainText('Only 3 left') // the hold is already reflected
  await page.screenshot(shot(testInfo.project.name === 'desktop' ? '3-checkout' : '3-checkout-phone'))

  // ...and survives a refresh too: the countdown resumes from the server's deadline.
  await page.reload()
  await expect(page.getByRole('timer')).toHaveText(/^1:[345]\d$/)

  await page.getByRole('button', { name: 'Pay $190.00' }).click()
  await expect(page.getByRole('heading', { name: 'You are going!' })).toBeVisible()
  await expect(page.getByRole('status').filter({ hasText: 'Order confirmed' })).toContainText('2 × GA · FLOOR')
  if (testInfo.project.name === 'desktop') await page.screenshot(shot('4-confirmed'))
})

test('abandoned checkout expires on screen and the tickets come back', async ({ page, request }) => {
  const eventId = await createEvent(request, { opensInSeconds: -5, holdSeconds: 6, floor: 2 })
  await page.goto(`/?event=${eventId}`)
  await page.getByRole('button', { name: 'Join the queue' }).click()
  await expect(page.getByRole('heading', { name: 'It is your turn' })).toBeVisible({ timeout: 20_000 })

  await page.getByLabel('Tickets').selectOption('2')
  await page.getByRole('button', { name: /^Hold 2/ }).click()
  await expect(page.getByRole('heading', { name: 'Checkout' })).toBeVisible()
  await expect(page.getByRole('row', { name: /GA · FLOOR/ })).toContainText('Sold out')

  // Do nothing. The client does not release the hold; the deadline and the sweeper do.
  await expect(page.getByRole('heading', { name: 'Your hold expired' })).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('row', { name: /GA · FLOOR/ })).toContainText('Only 2 left', { timeout: 10_000 })

  await page.getByRole('button', { name: 'Choose tickets again' }).click()
  await expect(page.getByRole('heading', { name: 'It is your turn' })).toBeVisible()
})

test('the whole purchase works from the keyboard', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name === 'phone', 'no hardware keyboard on the phone profile')
  const eventId = await createEvent(request, { opensInSeconds: -5, holdSeconds: 120, floor: 5 })
  await page.goto(`/?event=${eventId}`)

  await expect(page.getByRole('button', { name: 'Join the queue' })).toBeVisible() // rendered once our queue state is known
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Join the queue' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'It is your turn' })).toBeFocused({ timeout: 20_000 })

  await page.keyboard.press('Tab') // into the section radio group (GA FLOOR is preselected)
  await expect(page.getByRole('radio', { name: /GA · FLOOR/ })).toBeFocused()
  await page.keyboard.press('ArrowDown') // VIP BOX
  await expect(page.getByRole('radio', { name: /VIP · BOX/ })).toBeChecked()
  await page.keyboard.press('Tab') // quantity
  await page.keyboard.press('Tab') // submit
  await page.keyboard.press('Enter')

  await expect(page.getByRole('heading', { name: 'Checkout' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Pay $320.00' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'You are going!' })).toBeFocused()
})

test('unknown event is an explained dead end, not a blank page', async ({ page }) => {
  await page.goto(`/?event=${randomUUID()}`)
  await expect(page.getByRole('alert')).toContainText('That event does not exist')
})
