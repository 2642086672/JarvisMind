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
 * Replies that acknowledge but say nothing.
 *
 * Kept deliberately small and matched against the *whole* normalised message:
 * a substring rule would reject "谢谢，我叫年糕", and a length rule alone would
 * reject a short but real fact such as "我叫年糕".
 */
const PLEASANTRIES = new Set([
  '嗯', '嗯嗯', '哦', '噢', '啊', '好', '好的', '好吧', '行', '可以', '对', '对的', '是的',
  '谢谢', '多谢', '感谢', '谢谢你', '辛苦了', '收到', '明白了', '了解', '不错', '很好',
  'ok', 'okay', 'k', 'thanks', 'thank you', 'thx', 'got it', 'sure', 'yes', 'no',
  '你好', '您好', '嗨', 'hi', 'hello', '哈喽',
])

/** Trailing punctuation carries no meaning, so a reply of only punctuation is empty. */
const TRAILING_PUNCTUATION = /[。！？、，,.!?~～…·:：;；\s]+$/u

/**
 * Whether a user message is worth offering to the turn-end fallback.
 *
 * This is a local noise filter, not a judgement about what should be stored:
 * the server still applies its own intent, credential and sensitivity gates to
 * whatever is sent. Its only job is to stop a fallback from submitting "嗯" or
 * an emoji and calling that a memory attempt.
 */
export function isSubstantive(text: string): boolean {
  const normalized = text.replace(/[\s\u3000]+/g, ' ').trim()
  if (normalized.length === 0) return false
  const core = normalized.replace(TRAILING_PUNCTUATION, '').trim()
  if (core.length === 0) return false
  if (PLEASANTRIES.has(core.toLowerCase())) return false
  // One character is an acknowledgement, never a statement.
  if (core.length < 2) return false
  // No letter or digit at all means it is punctuation or an emoji.
  return /[\p{L}\p{N}]/u.test(core)
}

/**
 * Fire-and-forget capture of user turns.
 *
 * Nothing here is awaited by the chat path: `turnEnded` schedules the request
 * and returns immediately. Failures are contained and logged without content.
 * JarvisMind (JMR) decides whether the statement becomes a memory; every
 * automatic submission uses `mode: "auto"` so the server's own intent filter
 * still applies.
 *
 * In the default `rememberMode: 'agent'` the model decides what is worth
 * keeping. `fallbackCaptureOnTurnEnd` adds one safety net: if the model never
 * called `jarvismind_remember` during the turn, a substantive user message is
 * submitted once. That exists because a model absorbed in summarising a pasted
 * document can forget the tool call, and the user would never see the loss.
 */
export class CaptureQueue {
  private readonly pending = new Map<string, Map<number, PendingItem[]>>()
  /** The turn each agent is currently in, as reported by `agent/inbox/claimed`. */
  private readonly currentTurn = new Map<string, number>()
  /** Turns in which the model already called `jarvismind_remember`. */
  private readonly rememberedTurns = new Map<string, Set<number>>()
  private readonly inflight = new Set<Promise<void>>()

  constructor(
    private readonly http: JarvisMindHttp,
    private readonly config: ResolvedConfig,
    private readonly logger: LoggerLike,
  ) {}

  /** True when a completed turn is submitted regardless of what the model did. */
  private get capturesEveryTurn(): boolean {
    if (!this.config.autoRemember) return false
    return this.config.rememberMode === 'auto' || this.config.rememberMode === 'both'
  }

  /** True when the default `agent` mode keeps a turn in reserve for the fallback. */
  private get readyForFallback(): boolean {
    if (!this.config.autoRemember) return false
    return this.config.rememberMode === 'agent' && this.config.fallbackCaptureOnTurnEnd
  }

  /** True when a claimed message may need to be kept at all. */
  private get queuesTurns(): boolean {
    return this.capturesEveryTurn || this.readyForFallback
  }

  /** Observe a claimed inbox message and remember it for its turn. */
  noteClaimed(agentId: string, message: AgentMessage | undefined, turn: number): void {
    if (!this.queuesTurns) return
    this.currentTurn.set(agentId, turn)
    if (!isUserAuthored(message) || message === undefined) return
    const text = messageText(message)
    if (text.length === 0) return
    // The fallback is a safety net, not a crawler: it only offers a turn that
    // carries something. `auto`/`both` keep their original every-turn behavior
    // and let the server's intent filter decide.
    if (!this.capturesEveryTurn && !isSubstantive(text)) return

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

  /**
   * The model called `jarvismind_remember` during its current turn.
   *
   * The turn-end fallback must not then submit the same turn again. Recorded per
   * turn, not per agent, so a stale flag can never silence a later turn.
   */
  markRemembered(agentId: string | undefined): void {
    if (agentId === undefined) return
    const turn = this.currentTurn.get(agentId) ?? 0
    let turns = this.rememberedTurns.get(agentId)
    if (turns === undefined) {
      turns = new Set()
      this.rememberedTurns.set(agentId, turns)
    }
    turns.add(turn)
  }

  /** Submit the turn's user statements without blocking the reply. */
  turnEnded(agentId: string, conversationId: string, turn: number): void {
    const remembered = this.takeRemembered(agentId, turn)
    if (this.capturesEveryTurn) {
      this.flush(agentId, conversationId, (queued) => queued === turn)
      return
    }
    if (!this.readyForFallback) {
      this.drop(agentId, turn)
      return
    }
    if (remembered) {
      // The model already decided. Submitting the raw turn would duplicate it.
      this.drop(agentId, turn)
      return
    }
    this.flush(agentId, conversationId, (queued) => queued === turn)
  }

  /**
   * Flush every queued statement before the context is compacted.
   *
   * `compaction/start` is an observation event, not a cancellable hook, so this
   * cannot guarantee delivery: it reduces the window in which an un-flushed
   * statement would be summarised away. Anything already sent is unaffected.
   * A turn the model already remembered is dropped instead of sent twice.
   */
  compactionStarted(agentId: string, conversationId: string): void {
    if (!this.queuesTurns) return
    const remembered = this.rememberedTurns.get(agentId)
    if (remembered !== undefined) {
      const turns = this.pending.get(agentId)
      if (turns !== undefined) {
        for (const turn of [...turns.keys()]) {
          if (remembered.has(turn)) turns.delete(turn)
        }
        if (turns.size === 0) this.pending.delete(agentId)
      }
    }
    this.flush(agentId, conversationId, () => true)
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

  /** Forget all state for one agent (turn teardown / dispose). */
  clear(agentId: string): void {
    this.pending.delete(agentId)
    this.currentTurn.delete(agentId)
    this.rememberedTurns.delete(agentId)
  }

  /** Consume the "model remembered" marker for one turn. */
  private takeRemembered(agentId: string, turn: number): boolean {
    const turns = this.rememberedTurns.get(agentId)
    if (turns === undefined) return false
    const seen = turns.delete(turn)
    if (turns.size === 0) this.rememberedTurns.delete(agentId)
    return seen
  }

  /** Discard the queued statements for one turn. */
  private drop(agentId: string, turn: number): void {
    const turns = this.pending.get(agentId)
    if (turns === undefined) return
    turns.delete(turn)
    if (turns.size === 0) this.pending.delete(agentId)
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

  private send(input: { text: string; conversationId: string; messageId: string; turnId: string }): void {
    const task = (async () => {
      try {
        const receipt: RememberResult = await this.http.remember({
          ...input,
          // Explicit, not the server default: an automatic capture is a raw user
          // statement, so the server's intent and credential gates must judge it.
          mode: 'auto',
        })
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