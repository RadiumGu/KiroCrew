import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import EscalationCard from './EscalationCard'
import { deriveEscalationState, formatRemaining } from './escalationState'
import type { ChatMessage } from '../../types'

/** The read-only card renders a router <Link>; interactive cards need no router. */
function renderInRouter(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>)
}

function esc(meta: Record<string, unknown> = {}, content = '**Summary:** the deploy needs a go/no-go.\n\nTried: dry run passed.'): ChatMessage {
  return {
    role: 'escalation',
    cls: 'msg msg-escalation',
    content,
    ts: '2026-09-04T12:00:00Z',
    meta: {
      kind: 'escalation',
      escalation_id: 'e1',
      from_session: 'oncall-7',
      options: ['Ship it', 'Hold until Monday', 'Roll back'],
      mid: 'm-e1',
      ...meta,
    },
  }
}

function user(content: string, ts: string, meta?: Record<string, unknown>): ChatMessage {
  return { role: 'user', content, cls: 'msg msg-u', ts, meta }
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  window.sessionStorage.clear()
})

describe('EscalationCard', () => {
  it('renders the member title, the from line, the markdown body and the options as a single-choice list', () => {
    render(<EscalationCard message={esc({ goal: 'Weekly release' })} state="pending" memberName="oncall" onSend={() => {}} />)
    expect(screen.getByText('oncall needs you')).toBeInTheDocument()
    expect(screen.getByText('From oncall-7')).toBeInTheDocument()
    expect(screen.getByTestId('escalation-goal')).toHaveTextContent('Goal: Weekly release')
    expect(screen.getByTestId('escalation-body')).toHaveTextContent('the deploy needs a go/no-go')
    const list = screen.getByTestId('escalation-options')
    expect(list).toHaveAttribute('role', 'radiogroup')
    expect(list.className).toContain('flex-col')
    const items = within(list).getAllByRole('radio')
    expect(items.map(b => b.textContent)).toEqual(['Ship it', 'Hold until Monday', 'Roll back'])
    for (const b of items) expect(b).toHaveAttribute('aria-checked', 'false')
    // One roving tab stop: nothing selected → the first item.
    expect(items.map(b => b.tabIndex)).toEqual([0, -1, -1])
    expect(screen.getByText('Awaiting your reply')).toBeInTheDocument()
    expect(screen.getByTestId('escalation-reply-hint')).toHaveTextContent('Typing a message below also counts as your answer.')
    // Exactly one action control: the send button, disabled until a pick.
    expect(screen.getByTestId('escalation-send')).toBeDisabled()
  })

  it('with no member name the title names the sending session, and only falls back to the generic title without one', () => {
    renderInRouter(<EscalationCard message={esc()} state="pending" memberName="" />)
    expect(screen.getByText('oncall-7 needs you', { selector: 'span.font-semibold' })).toBeInTheDocument()
    // The "From …" line would repeat the title.
    expect(screen.queryByText('From oncall-7')).toBeNull()
    renderInRouter(<EscalationCard message={esc({ from_session: undefined })} state="pending" memberName="" />)
    expect(screen.getByText('Needs you', { selector: 'span.font-semibold' })).toBeInTheDocument()
  })

  it('one click selects (single-select), Send reply sends the option with the escalation id', () => {
    const onSend = vi.fn()
    render(<EscalationCard message={esc()} state="pending" memberName="oncall" onSend={onSend} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Ship it' }))
    fireEvent.click(screen.getByRole('radio', { name: 'Hold until Monday' }))
    expect(screen.getByRole('radio', { name: 'Hold until Monday' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: 'Ship it' })).toHaveAttribute('aria-checked', 'false')
    // The tab stop follows the selection.
    expect(screen.getByRole('radio', { name: 'Hold until Monday' }).tabIndex).toBe(0)
    expect(screen.getByRole('radio', { name: 'Ship it' }).tabIndex).toBe(-1)
    expect(onSend).not.toHaveBeenCalled()
    const send = screen.getByTestId('escalation-send')
    expect(send).toBeEnabled()
    fireEvent.click(send)
    expect(onSend).toHaveBeenCalledTimes(1)
    expect(onSend).toHaveBeenCalledWith('Hold until Monday', { escalation_id: 'e1' })
  })

  it('arrow keys move the selection within the group, Home/End jump to the ends', () => {
    render(<EscalationCard message={esc()} state="pending" memberName="oncall" onSend={() => {}} />)
    const first = screen.getByRole('radio', { name: 'Ship it' })
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(screen.getByRole('radio', { name: 'Hold until Monday' })).toHaveAttribute('aria-checked', 'true')
    expect(document.activeElement).toBe(screen.getByRole('radio', { name: 'Hold until Monday' }))
    fireEvent.keyDown(screen.getByRole('radio', { name: 'Hold until Monday' }), { key: 'End' })
    expect(screen.getByRole('radio', { name: 'Roll back' })).toHaveAttribute('aria-checked', 'true')
    fireEvent.keyDown(screen.getByRole('radio', { name: 'Roll back' }), { key: 'ArrowDown' })
    expect(screen.getByRole('radio', { name: 'Ship it' })).toHaveAttribute('aria-checked', 'true')
    fireEvent.keyDown(screen.getByRole('radio', { name: 'Ship it' }), { key: 'ArrowUp' })
    expect(screen.getByRole('radio', { name: 'Roll back' })).toHaveAttribute('aria-checked', 'true')
    fireEvent.keyDown(screen.getByRole('radio', { name: 'Roll back' }), { key: 'Home' })
    expect(screen.getByRole('radio', { name: 'Ship it' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: 'Ship it' }).tabIndex).toBe(0)
  })

  it('double-click on an option selects and sends at once', () => {
    const onSend = vi.fn()
    render(<EscalationCard message={esc()} state="pending" memberName="oncall" onSend={onSend} />)
    fireEvent.doubleClick(screen.getByRole('radio', { name: 'Roll back' }))
    expect(onSend).toHaveBeenCalledTimes(1)
    expect(onSend).toHaveBeenCalledWith('Roll back', { escalation_id: 'e1' })
  })

  it('a send latches the card: two rapid clicks fire once, controls lock, "Reply sent" shows', () => {
    const onSend = vi.fn()
    render(<EscalationCard message={esc()} state="pending" memberName="oncall" onSend={onSend} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Ship it' }))
    const send = screen.getByTestId('escalation-send')
    fireEvent.click(send)
    fireEvent.click(send)
    expect(onSend).toHaveBeenCalledTimes(1)
    expect(send).toBeDisabled()
    for (const b of screen.getAllByRole('radio')) expect(b).toBeDisabled()
    expect(screen.getByTestId('escalation-sent')).toHaveTextContent('Reply sent')
    expect(screen.queryByTestId('escalation-reply-hint')).toBeNull()
    // The double-click shortcut is latched too.
    fireEvent.doubleClick(screen.getByRole('radio', { name: 'Roll back' }))
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('the latch clears once the host derives the card as answered', () => {
    const onSend = vi.fn()
    const { rerender } = render(<EscalationCard message={esc()} state="pending" memberName="oncall" onSend={onSend} />)
    fireEvent.doubleClick(screen.getByRole('radio', { name: 'Ship it' }))
    expect(screen.getByTestId('escalation-sent')).toBeInTheDocument()
    rerender(<EscalationCard message={esc()} state="answered" memberName="oncall" onSend={onSend} />)
    expect(screen.queryByTestId('escalation-sent')).toBeNull()
    expect(screen.getByTestId('escalation-state-badge')).toHaveTextContent('Answered')
  })

  it('the selected option survives a remount (index-bearing list keys) and is cleared on send', () => {
    const onSend = vi.fn()
    const first = render(<EscalationCard message={esc()} state="pending" memberName="oncall" onSend={onSend} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Hold until Monday' }))
    first.unmount()
    render(<EscalationCard message={esc()} state="pending" memberName="oncall" onSend={onSend} />)
    expect(screen.getByRole('radio', { name: 'Hold until Monday' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('escalation-send')).toBeEnabled()
    fireEvent.click(screen.getByTestId('escalation-send'))
    expect(onSend).toHaveBeenCalledWith('Hold until Monday', { escalation_id: 'e1' })
    expect(window.sessionStorage.getItem('mc-escalation-selected:e1')).toBeNull()
  })

  it('read-only (no onSend): plain list, no buttons, no reply hint, links to the Members page', () => {
    renderInRouter(<EscalationCard message={esc()} state="pending" memberName="" />)
    const list = screen.getByTestId('escalation-options')
    expect(list.querySelectorAll('button')).toHaveLength(0)
    expect(list.querySelectorAll('[aria-checked]')).toHaveLength(0)
    expect(list).toHaveTextContent('Ship it')
    expect(screen.queryByTestId('escalation-send')).toBeNull()
    expect(screen.queryByTestId('escalation-reply-hint')).toBeNull()
    const note = screen.getByTestId('escalation-answer-in-thread')
    const link = within(note).getByRole('link', { name: "Answer this from the member's thread on the Crew Members page." })
    expect(link).toHaveAttribute('href', '/members')
  })

  it('past deadline with a default: defaulted, options disabled, send gone, default line in past tense', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-04T13:00:00Z'))
    const m = esc({ deadline: '2026-09-04T12:30:00Z', default_action: 'Hold the release' })
    const state = deriveEscalationState(m, [m], 0, Date.now())
    expect(state).toBe('defaulted')
    const onSend = vi.fn()
    render(<EscalationCard message={m} state={state} memberName="oncall" onSend={onSend} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'defaulted')
    for (const b of within(screen.getByTestId('escalation-options')).getAllByRole('radio')) expect(b).toBeDisabled()
    fireEvent.doubleClick(screen.getByRole('radio', { name: 'Ship it' }))
    expect(onSend).not.toHaveBeenCalled()
    expect(screen.queryByTestId('escalation-send')).toBeNull()
    expect(screen.getByTestId('escalation-expired')).toHaveTextContent('Window closed — default applies')
    expect(screen.getByTestId('escalation-default')).toHaveTextContent('Default applied: Hold the release')
    expect(screen.getByTestId('escalation-default')).not.toHaveTextContent('If you don’t answer')
    expect(screen.queryByTestId('escalation-deadline')).toBeNull()
    expect(screen.queryByTestId('escalation-reply-hint')).toBeNull()
  })

  it('expired without a default action says only that the window closed', () => {
    render(<EscalationCard message={esc({ deadline: '2020-01-01T00:00:00Z' })} state="expired" memberName="oncall" />)
    expect(screen.getByTestId('escalation-expired')).toHaveTextContent('Window closed')
    expect(screen.getByTestId('escalation-state-badge')).toHaveTextContent('Window closed')
    expect(screen.queryByTestId('escalation-default')).toBeNull()
  })

  it('pending with a deadline shows a live countdown that ticks every second', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-04T12:00:00Z'))
    render(<EscalationCard message={esc({ deadline: '2026-09-04T12:12:05Z' })} state="pending" memberName="oncall" onSend={() => {}} />)
    expect(screen.getByTestId('escalation-deadline')).toHaveTextContent('12m 05s left')
    act(() => { vi.advanceTimersByTime(5000) })
    expect(screen.getByTestId('escalation-deadline')).toHaveTextContent('12m 00s left')
  })

  it('the live clock crossing the deadline flips a pending card to defaulted without a re-render from the host', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-04T12:00:00Z'))
    const onSend = vi.fn()
    render(<EscalationCard message={esc({ deadline: '2026-09-04T12:00:03Z', default_action: 'Hold' })} state="pending" memberName="oncall" onSend={onSend} />)
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'pending')
    fireEvent.click(screen.getByRole('radio', { name: 'Ship it' }))
    expect(screen.getByTestId('escalation-send')).toBeEnabled()
    act(() => { vi.advanceTimersByTime(4000) })
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'defaulted')
    expect(screen.queryByTestId('escalation-send')).toBeNull()
    for (const b of within(screen.getByTestId('escalation-options')).getAllByRole('radio')) expect(b).toBeDisabled()
    fireEvent.doubleClick(screen.getByRole('radio', { name: 'Ship it' }))
    expect(onSend).not.toHaveBeenCalled()
    expect(screen.getByTestId('escalation-default')).toHaveTextContent('Default applied: Hold')
  })

  it('the 1s tick stops once the clock has flipped the card closed (interval keyed on the derived state)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-04T12:00:00Z'))
    const clearSpy = vi.spyOn(window, 'clearInterval')
    render(<EscalationCard message={esc({ deadline: '2026-09-04T12:00:02Z' })} state="pending" memberName="oncall" onSend={() => {}} />)
    expect(vi.getTimerCount()).toBe(1)
    act(() => { vi.advanceTimersByTime(3000) })
    expect(screen.getByTestId('escalation-card')).toHaveAttribute('data-state', 'expired')
    // The flip cleared the interval: no timers left, no more re-render reads.
    expect(clearSpy).toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
    const nowSpy = vi.spyOn(Date, 'now')
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(nowSpy).not.toHaveBeenCalled()
  })

  it('answered: badge says Answered, options disabled, no send, no default line', () => {
    render(<EscalationCard message={esc({ default_action: 'Hold' })} state="answered" memberName="oncall" onSend={() => {}} />)
    expect(screen.getByTestId('escalation-state-badge')).toHaveTextContent('Answered')
    for (const b of within(screen.getByTestId('escalation-options')).getAllByRole('radio')) expect(b).toBeDisabled()
    expect(screen.queryByTestId('escalation-send')).toBeNull()
    expect(screen.queryByTestId('escalation-reply-hint')).toBeNull()
    expect(screen.queryByTestId('escalation-default')).toBeNull()
  })
})

describe('deriveEscalationState', () => {
  const now = Date.parse('2026-09-04T12:00:00Z')
  it('single pending + free-text reply → answered, regardless of the deadline still being open', () => {
    const m = esc({ deadline: '2026-09-04T13:00:00Z' })
    const msgs: ChatMessage[] = [m, { role: 'tool', content: '🔧 x', cls: '' }, user('ship it', '2026-09-04T12:05:00Z')]
    expect(deriveEscalationState(m, msgs, 0, now)).toBe('answered')
  })
  it('two pending + free-text reply → both still pending', () => {
    const a = esc({ escalation_id: 'e1' })
    const b = esc({ escalation_id: 'e2' })
    const msgs: ChatMessage[] = [a, b, user("how's it going?", '2026-09-04T12:05:00Z')]
    expect(deriveEscalationState(a, msgs, 0, now)).toBe('pending')
    expect(deriveEscalationState(b, msgs, 1, now)).toBe('pending')
  })
  it('a reply scoped with meta.escalation_id answers only that one', () => {
    const a = esc({ escalation_id: 'e1' })
    const b = esc({ escalation_id: 'e2' })
    const msgs: ChatMessage[] = [a, b, user('Go', '2026-09-04T12:05:00Z', { escalation_id: 'e2' })]
    expect(deriveEscalationState(a, msgs, 0, now)).toBe('pending')
    expect(deriveEscalationState(b, msgs, 1, now)).toBe('answered')
    // Once e2 is gone, a later free-text reply has exactly one target left.
    const later = [...msgs, user('ship it', '2026-09-04T12:06:00Z')]
    expect(deriveEscalationState(a, later, 0, now)).toBe('answered')
  })
  it('a reply after the deadline does not answer: expired, or defaulted with a default action', () => {
    const m = esc({ deadline: '2026-09-04T11:00:00Z' })
    expect(deriveEscalationState(m, [m, user('ship it', '2026-09-04T11:30:00Z')], 0, now)).toBe('expired')
    const d = esc({ deadline: '2026-09-04T11:00:00Z', default_action: 'Hold' })
    expect(deriveEscalationState(d, [d, user('ship it', '2026-09-04T11:30:00Z', { escalation_id: 'e1' })], 0, now)).toBe('defaulted')
    // A reply sent exactly at the deadline is late too (on/before).
    expect(deriveEscalationState(m, [m, user('ship it', '2026-09-04T11:00:00Z')], 0, now)).toBe('expired')
  })
  it('an expired escalation no longer counts as pending for a free-text reply to the other one', () => {
    const stale = esc({ escalation_id: 'e1', deadline: '2026-09-04T11:00:00Z' })
    const live = esc({ escalation_id: 'e2' })
    const msgs: ChatMessage[] = [stale, live, user('Go', '2026-09-04T11:30:00Z')]
    expect(deriveEscalationState(stale, msgs, 0, now)).toBe('expired')
    expect(deriveEscalationState(live, msgs, 1, now)).toBe('answered')
  })
  it('is pending with no deadline, or a future one; closed once the deadline passes', () => {
    expect(deriveEscalationState(esc(), [esc()], 0, now)).toBe('pending')
    const future = esc({ deadline: '2026-09-04T13:00:00Z' })
    expect(deriveEscalationState(future, [future], 0, now)).toBe('pending')
    const past = esc({ deadline: '2026-09-04T11:00:00Z' })
    expect(deriveEscalationState(past, [past], 0, now)).toBe('expired')
  })
  it('ignores a user row BEFORE the escalation', () => {
    const m = esc()
    const msgs: ChatMessage[] = [user('go', '2026-09-04T11:00:00Z'), m]
    expect(deriveEscalationState(m, msgs, 1, now)).toBe('pending')
  })
  it('a merged queue-drain row with meta.escalation_ids answers each listed card; an unlisted one stays pending', () => {
    const a = esc({ escalation_id: 'A' })
    const b = esc({ escalation_id: 'B' })
    const c = esc({ escalation_id: 'C' })
    const merged = user('Ship it\nHold', '2026-09-04T12:05:00Z', { escalation_ids: ['A', 'B'] })
    const msgs: ChatMessage[] = [a, b, c, merged]
    expect(deriveEscalationState(a, msgs, 0, now)).toBe('answered')
    expect(deriveEscalationState(b, msgs, 1, now)).toBe('answered')
    expect(deriveEscalationState(c, msgs, 2, now)).toBe('pending')
    // C is now the only one open, so a later free-text reply answers it.
    const later = [...msgs, user('go', '2026-09-04T12:06:00Z')]
    expect(deriveEscalationState(c, later, 2, now)).toBe('answered')
    // A merged row does not answer a card whose window had already closed.
    const stale = esc({ escalation_id: 'S', deadline: '2026-09-04T11:00:00Z' })
    const live = esc({ escalation_id: 'L' })
    const late = user('x', '2026-09-04T11:30:00Z', { escalation_ids: ['S', 'L'] })
    expect(deriveEscalationState(stale, [stale, live, late], 0, now)).toBe('expired')
    expect(deriveEscalationState(live, [stale, live, late], 1, now)).toBe('answered')
    // An explicit list never falls back to the single-pending rule: unknown ids answer nothing.
    const only = esc({ escalation_id: 'X' })
    expect(deriveEscalationState(only, [only, user('y', '2026-09-04T12:05:00Z', { escalation_ids: ['nope'] })], 0, now)).toBe('pending')
  })
  it('an OPTIMISTIC user row (sent, not yet accepted by the server) does not answer; the same row confirmed does', () => {
    const m = esc()
    const optimistic = user('Ship it', '2026-09-04T12:05:00Z', { sendId: 's-1', optimistic: true })
    expect(deriveEscalationState(m, [m, optimistic], 0, now)).toBe('pending')
    // Scoped by escalation id, still not an answer while optimistic.
    const scoped = user('Ship it', '2026-09-04T12:05:00Z', { sendId: 's-2', escalation_id: 'e1', optimistic: true })
    expect(deriveEscalationState(m, [m, scoped], 0, now)).toBe('pending')
    // confirmOptimisticSend / the echo reconcile drop the flag — now it answers.
    const confirmed = user('Ship it', '2026-09-04T12:05:00Z', { sendId: 's-1' })
    expect(deriveEscalationState(m, [m, confirmed], 0, now)).toBe('answered')
    // An optimistic row does not shadow a later confirmed one either.
    expect(deriveEscalationState(m, [m, optimistic, confirmed], 0, now)).toBe('answered')
  })
  it('a user row with no parseable ts counts as sent now: it cannot answer a window that has already closed', () => {
    const past = esc({ deadline: '2026-09-04T11:00:00Z' })
    const noTs: ChatMessage = { role: 'user', content: 'ship it', cls: 'msg msg-u' }
    expect(deriveEscalationState(past, [past, noTs], 0, now)).toBe('expired')
    const badTs: ChatMessage = { role: 'user', content: 'ship it', cls: 'msg msg-u', ts: 'not-a-date', meta: { escalation_id: 'e1' } }
    expect(deriveEscalationState(past, [past, badTs], 0, now)).toBe('expired')
    // Still answers an open window (and one with no deadline at all).
    const open = esc({ deadline: '2026-09-04T13:00:00Z' })
    expect(deriveEscalationState(open, [open, noTs], 0, now)).toBe('answered')
    const none = esc()
    expect(deriveEscalationState(none, [none, noTs], 0, now)).toBe('answered')
  })
})

describe('formatRemaining', () => {
  it('formats minutes+seconds, hours+minutes, and days', () => {
    expect(formatRemaining(12 * 60_000 + 5_000)).toBe('12m 05s')
    expect(formatRemaining(2 * 3_600_000 + 10 * 60_000)).toBe('2h 10m')
    expect(formatRemaining(3 * 86_400_000 + 5_000)).toBe('3d')
    expect(formatRemaining(-500)).toBe('0m 00s')
  })
})
