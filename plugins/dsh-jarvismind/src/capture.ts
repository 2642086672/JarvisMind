import type { JarvisMindHttp, RememberResult } from './http.js'
import { isUserAuthored, messageText } from './messages.js'
import { safeErrorMessage } from './security.js'
import type { AgentMessage, LoggerLike, ResolvedConfig } from './types.js'

interface PendingItem {
  text: string
  messageId: string
}

const MAX_CAPTURE_CHARS = 5000

/**
 * Fire-and-forget capture of user turns.
 *
 * Nothing here is awaited by the chat path: `turnEnded` schedules the request
 * and returns immediately. Failures are contained and logged without content.
 * JarvisMind (JMR) decides whether the statement becomes a memory.
 *
 * In the default `rememberMode: 'agent'` this queue stays empty on purpose:
 * the model decides what is worth keeping and calls `jarvismind_remember`.
 * Submitting every turn mechanically is what produced noise like "嗯" and
 * "谢谢" as memory candidates.
 */
export class CaptureQueue {
  private readonly pending = new Map<string, Map<number, PendingItem[]>>()
  private readonly inflight = new Set<Promise<void>>()

  constructor(
    private readonly http: JarvisMindHttp,
    private readonly config: ResolvedConfig,
    private readonly logger: LoggerLike,
  ) {}

  /** True when a completed turn should be submitted without the model asking. */
  private get capturesEveryTurn(): boolean {
    if (!this.config.autoRemember) return false
    return this.config.rememberMode === 'auto' || this.config.rememberMode === 'both'
  }

  /** Observe a claimed inbox message and remember it for its turn. */
  noteClaimed(agentId: string, message: AgentMessage | undefined, turn: number): void {
    if (!this.capturesEveryTurn) return
    if (!isUserAuthored(message) || message === undefined) return
    const text = messageText(message)
    if (text.length === 0) return

    let turns = this.pending.get(agentId)
    if (turns === undefined) {
      turns = new Map()
      this.pending.set(agentId, turns)
    }
    const items = turns.get(turn) ?? []
    if (items.some((item) => item.messageId === message.id)) return
    items.push({ text: text.slice(0, MAX_CAPTURE_CHARS), messageId: message.id })
    turns.set(turn, items)
  }

  /** Submit the turn's user statements without blocking the reply. */
  turnEnded(agentId: string, conversationId: string, turn: number): void {
    if (!this.capturesEveryTurn) return
    this.flush(agentId, conversationId, (queued) => queued === turn)
  }

  /**
   * Flush every queued statement before the context is compacted.
   *
   * `compaction/start` is an observation event, not a cancellable hook, so this
   * cannot guarantee delivery: it reduces the window in which an un-flushed
   * statement would be summarised away. Anything already sent is unaffected.
   */
  compactionStarted(agentId: string, conversationId: string): void {
    if (!this.capturesEveryTurn) return
    this.flush(agentId, conversationId, () => true)
  }

  /** Submit matching queued statements without blocking the caller. */
  private flush(agentId: string, conversationId: string, matches: (turn: number) => boolean): void {
    const turns = this.pending.get(agentId)
    if (turns === undefined) return

    for (const [turn, items] of [...turns]) {
      if (!matches(turn)) continue
      turns.delete(turn)
      for (const item of items) {
        this.send({
          text: item.text,
          conversationId,
          messageId: item.messageId,
          turnId: String(turn),
        })
      }
    }
    if (turns.size === 0) this.pending.delete(agentId)
  }

  /** Await every in-flight submission (tests and orderly teardown). */
  async drain(): Promise<void> {
    while (this.inflight.size > 0) {
      await Promise.allSettled([...this.inflight])
    }
  }

  /** Number of queued statements, for diagnostics and tests. */
  pendingCount(): number {
    let count = 0
    for (const turns of this.pending.values()) {
      for (const items of turns.values()) count += items.length
    }
    return count
  }

  private send(input: { text: string; conversationId: string; messageId: string; turnId: string }): void {
    const task = (async () => {
      try {
        const receipt: RememberResult = await this.http.remember(input)
        this.logger.debug?.(`jarvismind: remember ${String(receipt?.status ?? 'unknown')}`)
      } catch (error) {
        this.logger.debug?.(`jarvismind: remember skipped (${safeErrorMessage(error)})`)
      }
    })()

    this.inflight.add(task)
    void task.finally(() => {
      this.inflight.delete(task)
    })
  }
}