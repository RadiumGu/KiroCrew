/**
 * Chat-profile renderer entries for a member DM thread.
 *
 * Supplied by ChatPane (displayProfile="chat") as host entries to the SDK's
 * row registry, on top of the store-connected transcript set. Three rows:
 * the escalation card (with a live send handler so its options reply), the
 * silent-rounds fold row, and an `assistant` override that DELEGATES to the
 * SDK's default reply bubble and appends the process disclosure the
 * projection attached to the row.
 */
import type { MessageRenderContext, MessageRenderer } from '../../app-sdk/messageRenderers'
import { defaultMessageRenderers } from '../../app-sdk/messageRenderers'
import EscalationCard from './EscalationCard'
import ChatFoldRow from './ChatFoldRow'
import ProcessDisclosure from './ProcessDisclosure'
import { deriveEscalationState } from './escalationState'

export interface ChatProfileRendererOptions {
  memberName: string
  /** `extra` is merged into the sent message's meta (the escalation id). */
  onSend?: (text: string, extra?: Record<string, unknown>) => void
}

export function createChatProfileRenderers({ memberName, onSend }: ChatProfileRendererOptions): MessageRenderer[] {
  const defaultAssistant = defaultMessageRenderers.find((r) => r.id === 'assistant')
  return [
    {
      id: 'escalation',
      roles: ['escalation'],
      render: (m, ctx) => ctx.row(
        <EscalationCard
          key={ctx.key}
          message={m}
          memberName={memberName}
          state={deriveEscalationState(m, ctx.messages, ctx.index, Date.now())}
          onSend={onSend}
        />,
        true,
      ),
    },
    {
      id: 'chat_fold',
      roles: ['chat_fold'],
      render: (m, ctx) => ctx.row(<ChatFoldRow key={ctx.key} message={m} />, true),
    },
    {
      // The SDK default draws the bubble (footer rule, variants, file chips —
      // one implementation, not a copy); this entry only adds the process
      // trail underneath, inside the same bubble layout. When the default
      // returns `null` (an invisible-only row — a quiet round) the tool steps
      // must still be reachable, so the trail is drawn on its own tight row.
      id: 'assistant',
      roles: ['assistant', 'streaming'],
      render: (m, ctx) => {
        if (!defaultAssistant) return null
        const meta = m.meta as Record<string, unknown> | undefined
        const process = Array.isArray(meta?.chat_process) ? (meta!.chat_process as unknown[]) : []
        if (process.length === 0) return defaultAssistant.render(m, ctx)
        const withProcess: MessageRenderContext = {
          ...ctx,
          wrapper: (children, isUser) => ctx.wrapper(
            <div className="flex flex-col gap-0">
              {children}
              <ProcessDisclosure message={m} />
            </div>,
            isUser,
          ),
        }
        const rendered = defaultAssistant.render(m, withProcess)
        if (rendered !== null) return rendered
        return ctx.row(<ProcessDisclosure key={ctx.key} message={m} />, true)
      },
    },
  ]
}
