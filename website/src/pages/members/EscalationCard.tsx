/**
 * EscalationCard — a crew member asking the person for a decision.
 *
 * Drawn for `escalation` rows (meta.kind === 'escalation'). The body is the
 * member's own markdown (summary / what was tried / what is needed); the card
 * adds the who/when frame around it: which member, from which session, the
 * decision window with a live countdown, the default that applies if nobody
 * answers, and the options as a single-choice list (same list QuestionCard
 * draws) with ONE send button that replies with the chosen option, tagged with
 * the escalation id so the backend answers exactly this request. State is
 * derived by the host from the transcript — see escalationState.ts — and the
 * card re-derives the closed states against its own ticking clock, so a
 * pending card flips to expired/defaulted the moment the deadline passes.
 *
 * Without `onSend` (SDK default renderer, main chat) the card is read-only: a
 * plain list and a link to the member's thread on the Members page, no dead
 * controls. With no member name the title names the sending session instead.
 */
import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { CircleAlert, CheckCircle2, Clock, MessageSquare, Target } from 'lucide-react'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import MessageErrorBoundary from '../../components/MessageErrorBoundary'
import { fmtDateTime } from '../../i18n/format'
import type { ChatMessage } from '../../types'
import { closedState, escalationDeadlineMs, escalationDefaultAction, formatRemaining, type EscalationState } from './escalationState'

export interface EscalationCardProps {
  message: ChatMessage
  state: EscalationState
  /** The member who asked; '' when the host cannot name one (generic title). */
  memberName: string
  /**
   * Sends the chosen option as the reply; `extra` carries the escalation id
   * for the message meta. Absent = read-only card.
   */
  onSend?: (text: string, extra?: Record<string, unknown>) => void
}

/**
 * The picked option outlives a remount: the list's React key in
 * ChatMessageList is index-bearing, so hydrating older rows remounts the card
 * and would drop `selected`. Kept per escalation id in sessionStorage — tab
 * scoped, cleared on send.
 */
const SELECTION_KEY = 'mc-escalation-selected:'

function readSelection(escalationId: string): string | null {
  if (!escalationId) return null
  try { return window.sessionStorage.getItem(SELECTION_KEY + escalationId) } catch { return null }
}

function writeSelection(escalationId: string, value: string | null): void {
  if (!escalationId) return
  try {
    if (value === null) window.sessionStorage.removeItem(SELECTION_KEY + escalationId)
    else window.sessionStorage.setItem(SELECTION_KEY + escalationId, value)
  } catch { /* storage unavailable: selection is simply per-mount */ }
}

export default function EscalationCard({ message, state: hostState, memberName, onSend }: EscalationCardProps) {
  const { t } = useTranslation()
  const meta = (message.meta ?? {}) as Record<string, unknown>
  const escalationId = typeof meta.escalation_id === 'string' ? meta.escalation_id : ''
  const fromSession = typeof meta.from_session === 'string' ? meta.from_session : ''
  const goal = typeof meta.goal === 'string' && meta.goal ? meta.goal : ''
  const defaultAction = escalationDefaultAction(message)
  const options = Array.isArray(meta.options)
    ? (meta.options as unknown[]).filter((o): o is string => typeof o === 'string' && o.trim().length > 0).slice(0, 6)
    : []
  const deadline = escalationDeadlineMs(message)
  // The host derives state when it renders the row; between rows only the
  // clock moves, so the pending→closed edge is re-read against the live tick.
  // The tick is keyed on the DERIVED pending state: when the tick itself flips
  // the card closed the host does not re-render, so an interval keyed on the
  // host's state would keep running — this one stops on the flip.
  const [now, setNow] = useState(() => Date.now())
  const state: EscalationState = hostState === 'pending' ? (closedState(message, now) ?? 'pending') : hostState
  const pending = state === 'pending'
  const closed = state === 'expired' || state === 'defaulted'
  const interactive = !!onSend
  const ticking = pending && deadline !== null
  useEffect(() => {
    if (!ticking) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [ticking])
  const [selected, setSelected] = useState<string | null>(() => {
    const stored = readSelection(escalationId)
    return stored !== null && options.includes(stored) ? stored : null
  })
  const pick = (o: string) => {
    setSelected(o)
    writeSelection(escalationId, o)
  }
  // In-flight latch: once a reply is on its way the controls lock and a small
  // note says so, until the transcript answers the card (a `user` row lands
  // and the host derives `answered`) or the deadline closes it. Held in a ref
  // as well so a double-click's second event cannot fire before the state
  // update lands.
  const [sent, setSent] = useState(false)
  const sentRef = useRef(false)
  useEffect(() => {
    if (!pending) { sentRef.current = false; setSent(false) }
  }, [pending])
  const locked = !pending || sent

  const send = (text: string) => {
    if (!pending || !onSend || sentRef.current) return
    sentRef.current = true
    setSent(true)
    writeSelection(escalationId, null)
    onSend(text, escalationId ? { escalation_id: escalationId } : undefined)
  }

  // Roving tabindex + arrow keys: the radiogroup is one tab stop; arrows move
  // the selection, Home/End jump to the ends.
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([])
  const onOptionKeyDown = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (locked || options.length === 0) return
    let next: number | null = null
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') next = (index + 1) % options.length
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') next = (index - 1 + options.length) % options.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = options.length - 1
    if (next === null) return
    e.preventDefault()
    pick(options[next])
    itemRefs.current[next]?.focus()
  }
  const selectedIndex = selected === null ? -1 : options.indexOf(selected)

  // Who is asking: the member by name; failing that (main chat surfaces, where
  // the host cannot name one) the session the request came from; a bare
  // "Needs you" only when neither is known.
  const title = memberName
    ? t('pages.members.chat.escalation_title', { member: memberName })
    : fromSession
      ? t('pages.members.chat.escalation_title_from', { session: fromSession })
      : t('pages.members.chat.escalation_title_generic')
  // The "From …" line adds nothing when the title already names the session.
  const showFrom = !!fromSession && !!memberName

  const closedLabel = state === 'defaulted'
    ? t('pages.members.chat.escalation_expired')
    : t('pages.members.chat.escalation_expired_no_default')
  const stateLabel = state === 'answered'
    ? t('pages.members.chat.escalation_answered')
    : closed
      ? closedLabel
      : t('pages.members.chat.escalation_pending')
  const stateColor = state === 'answered' ? 'var(--ok)' : 'var(--warn)'

  return (
    <div
      className="w-full min-w-0 rounded-md bg-card ring-1 ring-inset forced-colors:border ring-border text-text animate-scale-in"
      style={{ borderLeft: '3px solid var(--warn)' }}
      data-testid="escalation-card"
      data-state={state}
      role="group"
      aria-label={title}
    >
      <div className="flex items-start gap-2 px-3 pt-2.5 pb-1 min-w-0">
        <CircleAlert size={15} className="lucide-inline shrink-0 mt-0.5" style={{ color: 'var(--warn)' }} aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap min-w-0">
            <span className="text-[13px] leading-5 font-semibold truncate">{title}</span>
            <span
              className="text-[10px] leading-4 px-1.5 py-0.5 rounded-full shrink-0 inline-flex items-center gap-1"
              style={{ background: `color-mix(in srgb, ${stateColor} 18%, transparent)`, color: stateColor }}
              data-testid="escalation-state-badge"
            >
              {state === 'answered' && <CheckCircle2 size={10} aria-hidden="true" />}
              {stateLabel}
            </span>
          </div>
          {showFrom && (
            <div className="text-[11px] leading-4 text-muted truncate">
              {t('pages.members.chat.escalation_from', { session: fromSession })}
            </div>
          )}
        </div>
      </div>

      {goal && (
        <div className="px-3 pb-1">
          <span
            className="inline-flex items-center gap-1 text-[11px] leading-4 px-1.5 py-0.5 rounded-md text-muted ring-1 ring-inset ring-border max-w-full"
            data-testid="escalation-goal"
          >
            <Target size={10} className="shrink-0" aria-hidden="true" />
            <span className="truncate">{t('pages.members.chat.escalation_goal', { goal })}</span>
          </span>
        </div>
      )}

      {message.content && (
        <div className="msg-content px-3 py-1 text-sm leading-6 min-w-0 overflow-hidden" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }} data-testid="escalation-body">
          <MessageErrorBoundary rawContent={message.content}>
            <MarkdownRenderer content={message.content} softBreaks />
          </MessageErrorBoundary>
        </div>
      )}

      {(deadline !== null || defaultAction) && state !== 'answered' && (
        <div className="px-3 pt-1 pb-1 flex flex-col gap-0.5 text-[12px] leading-5 text-muted">
          {deadline !== null && pending && (
            <span className="inline-flex items-center gap-1.5" data-testid="escalation-deadline">
              <Clock size={11} className="shrink-0" aria-hidden="true" />
              {t('pages.members.chat.escalation_deadline', {
                time: fmtDateTime(deadline),
                remaining: formatRemaining(deadline - now),
              })}
            </span>
          )}
          {closed && (
            <span className="inline-flex items-center gap-1.5" style={{ color: 'var(--warn)' }} data-testid="escalation-expired">
              <Clock size={11} className="shrink-0" aria-hidden="true" />
              {closedLabel}
            </span>
          )}
          {defaultAction && (
            <span data-testid="escalation-default">
              {closed
                ? t('pages.members.chat.escalation_default_applied', { action: defaultAction })
                : t('pages.members.chat.escalation_default', { action: defaultAction })}
            </span>
          )}
        </div>
      )}

      {options.length > 0 && (interactive ? (
        // Single-choice list, same item grammar as QuestionCard's options: one
        // click selects, the send button below replies; a double-click is the
        // select-and-send shortcut for people who know it. ARIA radiogroup:
        // each item is a radio with aria-checked, one roving tab stop.
        <div className="flex flex-col gap-1.5 px-3 pt-1 pb-2" role="radiogroup" aria-label={title} data-testid="escalation-options">
          {options.map((o, i) => {
            const isSelected = selected === o
            const tabStop = selectedIndex === -1 ? i === 0 : isSelected
            return (
              <button
                key={i + ':' + o}
                ref={(el) => { itemRefs.current[i] = el }}
                type="button"
                role="radio"
                aria-checked={isSelected}
                tabIndex={tabStop ? 0 : -1}
                disabled={locked}
                onClick={() => pick(o)}
                onDoubleClick={() => { pick(o); send(o) }}
                onKeyDown={(e) => onOptionKeyDown(e, i)}
                className={`text-left px-3 py-2 rounded-lg text-[13px] cursor-pointer transition-all border disabled:opacity-50 disabled:cursor-default ${
                  isSelected
                    ? 'border-accent text-text bg-accent-subtle/60'
                    : 'border-border text-muted hover:text-text hover:border-accent/40 bg-bg'
                }`}
              >
                <span className="font-medium">{o}</span>
              </button>
            )
          })}
        </div>
      ) : (
        <ul className="flex flex-col gap-1 px-3 pt-1 pb-2 list-none m-0 text-[12px] leading-5 text-muted" data-testid="escalation-options" aria-label={title}>
          {options.map((o, i) => (
            <li key={i + ':' + o} className="min-w-0 truncate">{o}</li>
          ))}
        </ul>
      ))}

      {pending && interactive && (
        <div className="px-3 pb-2.5 flex items-center gap-3 flex-wrap">
          <button
            type="button"
            onClick={() => { if (selected) send(selected) }}
            disabled={!selected || sent}
            data-testid="escalation-send"
            className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-md text-[13px] font-medium cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
          >
            <MessageSquare size={14} aria-hidden="true" /> {t('pages.members.chat.escalation_send')}
          </button>
          {sent ? (
            <span className="text-[11px] leading-4 text-muted/80" data-testid="escalation-sent" role="status">
              {t('pages.members.chat.escalation_sent')}
            </span>
          ) : (
            <span className="text-[11px] leading-4 text-muted/80" data-testid="escalation-reply-hint">
              {t('pages.members.chat.escalation_type_instead')}
            </span>
          )}
        </div>
      )}

      {pending && !interactive && (
        // A real navigation, not a hint: the answer lives on the Members page.
        <div className="px-3 pb-2 text-[11px] leading-4" data-testid="escalation-answer-in-thread">
          <Link to="/members" className="text-accent hover:underline">
            {t('pages.members.chat.escalation_answer_in_thread')}
          </Link>
        </div>
      )}
    </div>
  )
}
