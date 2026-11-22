import type { AgentMessage } from './types.js'

/**
 * Whether a claimed inbox message was written by the human user.
 *
 * The DSH Web/Desktop prompt path tags its messages with `source.kind: 'user'`
 * (`dsh-api-session-controller`). Plugin-injected context uses `plugin`, PTC
 * image context uses `ptc-mode`, and runtime context is appended outside the
 * inbox, so this filter keeps recall and capture on real user input.
 */
export function isUserAuthored(message: AgentMessage | undefined): boolean {
  if (message === undefined || message.role !== 'user') return false
  return message.source?.kind === 'user' || message.source?.kind === undefined
}

/** Flatten the text blocks of one message, ignoring attachments and tool parts. */
export function messageText(message: AgentMessage | undefined): string {
  if (message === undefined || !Array.isArray(message.content)) return ''
  const parts: string[] = []
  for (const block of message.content) {
    if (block?.type === 'text' && typeof block.text === 'string' && block.text.trim().length > 0) {
      parts.push(block.text.trim())
    }
  }
  return parts.join('\n').trim()
}