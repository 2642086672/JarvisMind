import type { JarvisMindHttp } from './http.js'
import { isUserAuthored, messageText } from './messages.js'
import { safeErrorMessage } from './security.js'
import type { AgentMessage, LoggerLike, ResolvedConfig } from './types.js'

interface RecallSlot {
  messageId: string
  promise: Promise<string | undefined>
}

const MAX_QUERY_CHARS = 500

/**
 * One recall per user message, shared by every step of that turn.
 *
 * Starting the HTTP request when the inbox claims the message overlaps the
 * call with prompt assembly; all later steps of the same turn reuse the same
 * promise so the injected prompt stays byte-stable across tool iterations.
 */
export class RecallCoordinator {
  private readonly slots = new Map<string, RecallSlot>()

  constructor(
    private readonly http: JarvisMindHttp,
    private readonly config: ResolvedConfig,
    private readonly logger: LoggerLike,
  ) {}

  /** Observe a claimed inbox message. Non-user messages are ignored. */
  noteClaimed(agentId: string, message: AgentMessage | undefined): void {
    if (!this.config.autoRecall) return
    if (!isUserAuthored(message) || message === undefined) return
    const current = this.slots.get(agentId)
    if (current?.messageId === message.id) return

    const text = messageText(message)
    if (text.length === 0) return

    const query = text.slice(0, MAX_QUERY_CHARS)
    this.slots.set(agentId, {
      messageId: message.id,
      promise: this.fetchContext(query),
    })
  }

  /** The context promise for this agent's current user message, if any. */
  contextFor(agentId: string): Promise<string | undefined> | undefined {
    return this.slots.get(agentId)?.promise
  }

  /** Drop the cached recall when the turn or the agent ends. */
  clear(agentId: string): void {
    this.slots.delete(agentId)
  }

  private async fetchContext(query: string): Promise<string | undefined> {
    try {
      const result = await this.http.context(query, this.config.recallLimit)
      return result.context
    } catch (error) {
      // Fail open: a missing server must never block or break the chat.
      this.logger.debug?.(`jarvismind: recall skipped (${safeErrorMessage(error)})`)
      return undefined
    }
  }
}