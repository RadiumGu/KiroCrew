/**
 * Escalation lifecycle, derived on the client from the transcript.
 *
 * The backend writes an `escalation` row once and never edits it; whether it
 * has been dealt with follows from what comes after it. The answer rule here
 * is a SIMULATION over the message list and mirrors the backend's exactly:
 *
 * - an `escalation` row becomes PENDING (with its deadline);
 * - a `user` row still marked `meta.optimistic` (sent, not yet accepted by the
 *   server) answers nothing — a refused send must not close the card;
 * - a `user` row cannot answer an escalation whose deadline had already passed
 *   when the reply was sent (the backend has recorded it defaulted/expired);
 *   a row with no parseable `ts` counts as sent now;
 * - a `user` row carrying `meta.escalation_id` answers exactly that one; a
 *   merged row (several chip replies drained at once) carrying
 *   `meta.escalation_ids: string[]` answers each listed one;
 * - a `user` row WITHOUT one answers the pending escalation only when exactly
 *   one is pending at that moment — an unrelated "how's it going?" while two
 *   members wait must not flip both cards;
 * - anything still pending past its deadline is `expired`, or `defaulted`
 *   when the member recorded a default action.
 */
import type { ChatMessage } from '../../types'
import { fmtUnit } from '../../i18n/format'

export type EscalationState = 'pending' | 'answered' | 'expired' | 'defaulted'

export function escalationDeadlineMs(m: ChatMessage): number | null {
  const raw = m.meta?.deadline
  if (typeof raw !== 'string' || !raw) return null
  const t = Date.parse(raw)
  return Number.isFinite(t) ? t : null
}

/** The recorded default action, or '' when the member did not name one. */
export function escalationDefaultAction(m: ChatMessage): string {
  const raw = m.meta?.default_action
  return typeof raw === 'string' ? raw.trim() : ''
}

/** Identity of an escalation row: its id, or its position when it has none. */
function escalationKey(m: ChatMessage, index: number): string {
  const id = m.meta?.escalation_id
  return typeof id === 'string' && id ? id : `#${index}`
}

function rowTs(m: ChatMessage): number | null {
  if (typeof m.ts !== 'string' || !m.ts) return null
  const t = Date.parse(m.ts)
  return Number.isFinite(t) ? t : null
}

/** The state the card at `index` is in at `now`, per the shared answer rule. */
export function deriveEscalationState(
  m: ChatMessage,
  allMessages: readonly ChatMessage[],
  index: number,
  now: number,
): EscalationState {
  const mine = escalationKey(m, index)
  const pending = new Map<string, number | null>()
  let answered = false
  for (let j = 0; j < allMessages.length; j++) {
    const row = allMessages[j]
    if (row.role === 'escalation') {
      pending.set(escalationKey(row, j), escalationDeadlineMs(row))
      continue
    }
    if (row.role !== 'user') continue
    // An OPTIMISTIC bubble (appended client-side at send time, cleared by the
    // store once the server accepts the send) is not an answer yet: a refused
    // send must leave the card pending so the person can retry.
    if (row.meta?.optimistic === true) continue
    // A late reply cannot answer: drop everything whose window had closed by
    // the time this row was sent. A row with no parseable `ts` is taken as
    // sent NOW — it can never answer a record whose deadline already passed.
    const sentAt = rowTs(row) ?? now
    for (const [key, deadline] of pending) {
      if (deadline !== null && deadline <= sentAt) pending.delete(key)
    }
    // Which escalation(s) this row names: a single chip reply carries
    // `escalation_id`; the queue drain merges several chip replies queued while
    // the member was busy into ONE row carrying `escalation_ids: string[]`,
    // and that row answers EACH listed id.
    const targets = escalationTargets(row)
    if (targets.length > 0) {
      for (const target of targets) {
        if (!pending.has(target)) continue
        pending.delete(target)
        if (target === mine) answered = true
      }
      continue
    }
    // Free text: answers the pending escalation only when exactly one is open.
    if (pending.size !== 1) continue
    const hit = pending.keys().next().value as string
    pending.delete(hit)
    if (hit === mine) answered = true
  }
  if (answered) return 'answered'
  return closedState(m, now) ?? 'pending'
}

/** The escalation ids a user row explicitly names (`escalation_ids` merged row, else `escalation_id`). */
function escalationTargets(row: ChatMessage): string[] {
  const many = row.meta?.escalation_ids
  if (Array.isArray(many)) {
    const ids = many.filter((id): id is string => typeof id === 'string' && id.length > 0)
    if (ids.length > 0) return ids
  }
  const one = row.meta?.escalation_id
  return typeof one === 'string' && one ? [one] : []
}

/**
 * `expired` / `defaulted` once the clock has passed the row's deadline, else
 * null. The card uses this against its ticking clock so a pending card flips
 * the moment the window closes, without waiting for a new row.
 */
export function closedState(m: ChatMessage, now: number): Extract<EscalationState, 'expired' | 'defaulted'> | null {
  const deadline = escalationDeadlineMs(m)
  if (deadline === null || deadline > now) return null
  return escalationDefaultAction(m) ? 'defaulted' : 'expired'
}

/**
 * Compact localized countdown: `12m 05s` under an hour, `2h 10m` under a day,
 * `3d` beyond. Negative input clamps to zero.
 */
export function formatRemaining(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  if (total < 3600) {
    const m = Math.floor(total / 60)
    const s = total % 60
    return fmtUnit(m, 'minute') + ' ' + fmtUnit(s, 'second', { minimumIntegerDigits: 2 })
  }
  if (total < 86400) {
    const h = Math.floor(total / 3600)
    const m = Math.floor((total % 3600) / 60)
    return fmtUnit(h, 'hour') + ' ' + fmtUnit(m, 'minute')
  }
  return fmtUnit(Math.floor(total / 86400), 'day')
}
