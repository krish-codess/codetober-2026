import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, type Availability, type QueueStatus, type Reservation } from './api'
import { Checkout } from './Checkout'
import { Queue } from './Queue'
import { AvailabilityTable, TicketPicker } from './Tickets'
import { clock, roughWait } from './time'

const NOW = Date.parse('2026-10-10T10:00:00Z')
const iso = (offsetSeconds: number) => new Date(NOW + offsetSeconds * 1000).toISOString()

const reservation = (secondsLeft: number): Reservation => ({
  id: 'aaaaaaaa-0000-4000-8000-000000000000',
  inventoryId: 'inv-floor',
  eventId: 'evt',
  quantity: 2,
  status: 'HELD',
  expiresAt: iso(secondsLeft),
  createdAt: iso(secondsLeft - 120),
  serverTime: iso(0),
})
const availability: Availability = {
  eventId: 'evt',
  asOf: iso(0),
  sections: [
    { inventoryId: 'inv-box', ticketType: 'VIP', section: 'BOX', priceCents: 32000, available: 0, total: 40 },
    { inventoryId: 'inv-floor', ticketType: 'GA', section: 'FLOOR', priceCents: 9500, available: 3, total: 400 },
  ],
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(NOW)
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('formatting', () => {
  it('formats countdowns and never goes negative', () => {
    expect(clock(125)).toBe('2:05')
    expect(clock(59.2)).toBe('1:00')
    expect(clock(3723)).toBe('1:02:03')
    expect(clock(-5)).toBe('0:00')
  })
  it('gives deliberately coarse wait estimates', () => {
    expect(roughWait(3)).toBe('a few seconds')
    expect(roughWait(34)).toBe('about 30 seconds')
    expect(roughWait(70)).toBe('about a minute')
    expect(roughWait(600)).toBe('about 10 minutes')
    expect(roughWait(9000)).toBe('more than an hour')
  })
})

describe('Checkout', () => {
  const setup = (secondsLeft: number) => {
    const onConfirmed = vi.fn()
    const onGone = vi.fn()
    render(<Checkout reservation={reservation(secondsLeft)} section={availability.sections[1]} holdSeconds={120}
      headingRef={null} onConfirmed={onConfirmed} onGone={onGone} />)
    return { onConfirmed, onGone, user: userEvent.setup({ advanceTimers: vi.advanceTimersByTime }) }
  }

  it('counts down against the hold deadline and shows what is being bought', () => {
    setup(95)
    expect(screen.getByTestId('countdown')).toHaveTextContent('1:35')
    expect(screen.getByText(/2 × GA · FLOOR/)).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(40_000) })
    expect(screen.getByTestId('countdown')).toHaveTextContent('0:55')
    expect(screen.getByText('One minute left to pay.')).toBeInTheDocument()
  })

  it('uses the server clock, not the device clock', () => {
    vi.setSystemTime(NOW + 3_600_000) // device is an hour fast; serverTime in the reservation says otherwise
    setup(90)
    expect(screen.getByTestId('countdown')).toHaveTextContent('1:30')
  })

  it('flips to an explicit expired state at zero, with a way forward', async () => {
    const { onGone, user } = setup(2)
    act(() => { vi.advanceTimersByTime(2500) })
    expect(screen.getByRole('alert')).toHaveTextContent('Time ran out')
    expect(screen.queryByRole('button', { name: /pay/i })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Choose tickets again' }))
    expect(onGone).toHaveBeenCalled()
  })

  it('confirms the purchase', async () => {
    const confirmed = { ...reservation(90), status: 'CONFIRMED' as const }
    vi.spyOn(api, 'confirm').mockResolvedValue(confirmed)
    const { onConfirmed, user } = setup(90)
    await user.click(screen.getByRole('button', { name: 'Pay $190.00' }))
    expect(onConfirmed).toHaveBeenCalledWith(confirmed)
  })

  it('treats a server-side expiry as expired even if the local clock disagrees', async () => {
    vi.spyOn(api, 'confirm').mockRejectedValue(new ApiError(410, 'HOLD_EXPIRED', 'Your hold expired.'))
    const { onConfirmed, user } = setup(90)
    await user.click(screen.getByRole('button', { name: /pay/i }))
    expect(await screen.findByText('Your hold expired')).toBeInTheDocument()
    expect(onConfirmed).not.toHaveBeenCalled()
  })

  it('explains a transient failure and lets the user retry', async () => {
    const confirm = vi.spyOn(api, 'confirm').mockRejectedValueOnce(new ApiError(503, 'DEPENDENCY_UNAVAILABLE', 'We could not reach our database.'))
      .mockResolvedValue({ ...reservation(90), status: 'CONFIRMED' })
    const { onConfirmed, user } = setup(90)
    await user.click(screen.getByRole('button', { name: /pay/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('safe to try again')
    await user.click(screen.getByRole('button', { name: /pay/i }))
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(onConfirmed).toHaveBeenCalled()
  })

  it('releases the tickets on request', async () => {
    const release = vi.spyOn(api, 'release').mockResolvedValue({ ...reservation(90), status: 'RELEASED' })
    const { onGone, user } = setup(90)
    await user.click(screen.getByRole('button', { name: 'Release tickets' }))
    expect(release).toHaveBeenCalledWith(reservation(90).id)
    expect(onGone).toHaveBeenCalled()
  })
})

describe('Queue', () => {
  const show = (status: Partial<QueueStatus>, extra: Partial<Parameters<typeof Queue>[0]> = {}) =>
    render(<Queue status={{ state: 'QUEUED', onSaleAt: iso(-10), pollAfterMs: 1000, ...status }} pollError={null} rejoined={false}
      clockOffsetMs={0} headingRef={null} {...extra} />)

  it('promises no position in the lobby, and says why', () => {
    show({ state: 'LOBBY', onSaleAt: iso(75), estimatedWaitSeconds: 75 })
    expect(screen.getByRole('timer')).toHaveTextContent('1:15')
    expect(screen.getByText(/random place in line/)).toBeInTheDocument()
    expect(screen.queryByTestId('position')).not.toBeInTheDocument()
  })

  it('shows position, people ahead and a coarse estimate', () => {
    show({ position: 1201, estimatedWaitSeconds: 25 }, { availableNow: 5000 })
    expect(screen.getByTestId('position')).toHaveTextContent('1,201')
    expect(screen.getByText(/1,200 people ahead of you/)).toBeInTheDocument()
    expect(screen.getByText('about 30 seconds')).toBeInTheDocument()
    expect(screen.getByText(/fewer people ahead of you than tickets left/)).toBeInTheDocument()
  })

  it('is honest when the odds are bad or everything is gone', () => {
    const { rerender } = show({ position: 900, estimatedWaitSeconds: 18 }, { availableNow: 40 })
    expect(screen.getByText(/may sell out before your turn/)).toBeInTheDocument()
    rerender(<Queue status={{ state: 'QUEUED', position: 900, estimatedWaitSeconds: 18, onSaleAt: iso(-10), pollAfterMs: 1000 }}
      availableNow={0} pollError={null} rejoined={false} clockOffsetMs={0} headingRef={null} />)
    expect(screen.getByText(/Nothing available right now/)).toBeInTheDocument()
    expect(screen.getByText(/you keep your place if you wait/)).toBeInTheDocument()
  })

  it('keeps the last known position visible, marked stale, when polling fails', () => {
    show({ position: 42, estimatedWaitSeconds: 1 }, { pollError: new ApiError(503, 'WAITING_ROOM_UNAVAILABLE', 'down') })
    expect(screen.getByTestId('position')).toHaveTextContent('42')
    expect(screen.getByText(/Lost contact with the queue/)).toBeInTheDocument()
  })

  it('progress bar reflects real movement through the line', () => {
    const { rerender } = show({ position: 100, estimatedWaitSeconds: 2 })
    rerender(<Queue status={{ state: 'QUEUED', position: 26, estimatedWaitSeconds: 1, onSaleAt: iso(-10), pollAfterMs: 1000 }}
      pollError={null} rejoined={false} clockOffsetMs={0} headingRef={null} />)
    const bar = screen.getByRole('progressbar', { name: 'Progress through the line' }) as HTMLProgressElement
    expect(bar.max).toBe(100)
    expect(bar.value).toBe(75)
  })
})

describe('TicketPicker', () => {
  const setup = (data: Availability | undefined = availability) => {
    const onHeld = vi.fn()
    const onStale = vi.fn()
    render(<TicketPicker availability={data} maxPerUser={4} admissionToken="adm" headingRef={null} onHeld={onHeld} onStale={onStale} />)
    return { onHeld, onStale, user: userEvent.setup({ advanceTimers: vi.advanceTimersByTime }) }
  }

  it('disables sold-out sections and caps quantity at what is left', () => {
    setup()
    expect(screen.getByRole('radio', { name: /VIP · BOX/ })).toBeDisabled()
    expect(screen.getByRole('radio', { name: /GA · FLOOR/ })).toBeChecked()
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['1', '2', '3'])
  })

  it('reuses the idempotency key when retrying a transient failure, and rotates it after a refusal', async () => {
    const reserve = vi.spyOn(api, 'reserve')
      .mockRejectedValueOnce(new ApiError(503, 'CONTENTION', 'Lots of people are buying right now.'))
      .mockRejectedValueOnce(new ApiError(409, 'SOLD_OUT', 'Only 1 left in this section.'))
      .mockResolvedValue(reservation(120))
    const { onHeld, user } = setup()
    await user.selectOptions(screen.getByLabelText('Tickets'), '2')
    const hold = () => user.click(screen.getByRole('button', { name: /^Hold 2/ }))

    await hold()
    expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was reserved')
    await hold()
    expect(await screen.findByRole('alert')).toHaveTextContent('Someone got there first')
    await hold()

    const keys = reserve.mock.calls.map((c) => c[3])
    expect(reserve.mock.calls[0]!.slice(0, 3)).toEqual(['inv-floor', 2, 'adm'])
    expect(keys[1]).toBe(keys[0])
    expect(keys[2]).not.toBe(keys[1])
    expect(onHeld).toHaveBeenCalledWith(reservation(120))
  })

  it('asks the parent to re-read server state when admission has lapsed', async () => {
    vi.spyOn(api, 'reserve').mockRejectedValue(new ApiError(403, 'NOT_ADMITTED', 'Your admission expired. Rejoin the queue.'))
    const { onStale, user } = setup()
    await user.click(screen.getByRole('button', { name: /^Hold 1/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Rejoin the queue')
    expect(onStale).toHaveBeenCalled()
  })

  it('says so when it is your turn but everything is gone', () => {
    setup({ ...availability, sections: availability.sections.map((s) => ({ ...s, available: 0 })) })
    expect(screen.getByText(/It is your turn, but nothing is available/)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})

describe('AvailabilityTable', () => {
  it('has distinct loading, error, empty, populated and stale states', () => {
    const { rerender } = render(<AvailabilityTable loading error={false} />)
    expect(screen.getByLabelText('Loading availability')).toHaveAttribute('aria-busy', 'true')
    rerender(<AvailabilityTable loading={false} error />)
    expect(screen.getByRole('alert')).toHaveTextContent('could not be loaded')
    rerender(<AvailabilityTable loading={false} error={false} data={{ ...availability, sections: [] }} />)
    expect(screen.getByText(/No tickets have been put on sale/)).toBeInTheDocument()
    rerender(<AvailabilityTable loading={false} error data={availability} />)
    expect(screen.getByText('Sold out')).toBeInTheDocument() // text, not just a red cell
    expect(screen.getByText('Only 3 left')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('may be a few seconds old')
  })
})
