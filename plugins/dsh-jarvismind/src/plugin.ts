import z from '@deepseek-ai/schemastery'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import {
  DEFAULT_CORE_ANCHOR_LIMIT,
  DEFAULT_CORE_ANCHOR_MAX_CHARS,
  DEFAULT_BASE_URL,
  DEFAULT_MEMORY_STYLE,
  DEFAULT_RECALL_LIMIT,
  DEFAULT_REMEMBER_MODE,
  DEFAULT_TIMEOUT_MS,
  DEFAULT_TOKEN_ENV,
  DEFAULT_TOKEN_FILE,
  MEMORY_STYLES,
  REMEMBER_MODES,
  normalizeConfig,
} from './config.js'
import { CaptureQueue } from './capture.js'
import { CoreAnchorCache } from './core-anchor.js'
import { JarvisMindHttp } from './http.js'
import { memoryGuide } from './memory-style.js'
import {
  buildAnchorSection,
  buildContextSection,
  buildMemoryGuideSection,
  contextEntryKeys,
  mergePromptSections,
} from './prompt.js'
import { RecallCoordinator } from './recall.js'
import { publicUrl } from './security.js'
import { resolveToken } from './token.js'
import { createJarvisMindTools } from './tools.js'
import type { AgentLike, AgentMessage, AgentSession, LoggerLike, PluginConfig } from './types.js'

/** Cordis row config. Defaults are the documented install-time behavior. */
export const Config = z.object({
  baseUrl: z.string().default(DEFAULT_BASE_URL),
  tokenFile: z.string().default(DEFAULT_TOKEN_FILE),
  tokenEnv: z.string().default(DEFAULT_TOKEN_ENV),
  autoRecall: z.boolean().default(true),
  recallLimit: z.number().default(DEFAULT_RECALL_LIMIT),
  autoRemember: z.boolean().default(true),
  rememberMode: z.union([...REMEMBER_MODES]).default(DEFAULT_REMEMBER_MODE),
  fallbackCaptureOnTurnEnd: z.boolean().default(true),
  memoryStyle: z.union([...MEMORY_STYLES]).default(DEFAULT_MEMORY_STYLE),
  coreAnchorEnabled: z.boolean().default(true),
  coreAnchorLimit: z.number().default(DEFAULT_CORE_ANCHOR_LIMIT),
  coreAnchorMaxChars: z.number().default(DEFAULT_CORE_ANCHOR_MAX_CHARS),
  timeoutMs: z.number().default(DEFAULT_TIMEOUT_MS),
})

/** Services this plugin cannot work without. */
export const inject = ['systemPrompt', 'agents']

interface ToolRegistryLike {
  register(definition: ToolDefinition): () => void
}

interface AgentRegistryLike {
  roots(): AgentLike[]
}

interface PluginContext {
  logger: LoggerLike
  agents: AgentRegistryLike
  tools: ToolRegistryLike
  on(event: string, listener: (...args: any[]) => any): () => void
  effect(setup: () => unknown): () => void
  inject(dependencies: string[], callback: (ctx: PluginContext) => void): unknown
}

interface ClaimedPayload {
  message?: AgentMessage
  turn?: number
}

interface AgentPayload {
  agent?: AgentLike
}

interface SessionEventLike {
  type?: string
  data?: { turn?: number; [key: string]: unknown }
}

/**
 * Native DSH (Cordis) plugin entry.
 *
 * Recall is a scoped `system-prompt/assemble` waterfall on each top-level
 * agent; capture observes the durable `turn/end` event. Both are fail-open.
 * The three explicit tools register only when `ctx.tools` exists.
 */
export function apply(ctx: PluginContext, rawConfig?: PluginConfig): void {
  const config = normalizeConfig(rawConfig)
  const http = new JarvisMindHttp(config.baseUrl, () => resolveToken(config), { timeoutMs: config.timeoutMs })
  const recall = new RecallCoordinator(http, config, ctx.logger)
  const capture = new CaptureQueue(http, config, ctx.logger)
  const anchor = new CoreAnchorCache(http, config, ctx.logger)
  // The brief only makes sense when the model is the one choosing. In `auto`
  // mode every turn is already submitted, so a "remember this" instruction
  // would only cause the same statement to be sent twice.
  const guide =
    config.rememberMode === 'agent' || config.rememberMode === 'both'
      ? buildMemoryGuideSection(memoryGuide(config.memoryStyle))
      : undefined
  const disposers = new Map<string, () => void>()
  const agentsBySession = new Map<string, AgentLike>()

  const setupAgent = (agent: AgentLike): void => {
    if (disposers.has(agent.id)) return
    // Only top-level conversations participate. In-process subagents must not
    // write their internal prompts into the user's long-term memory.
    if (!ctx.agents.roots().some((root) => root.id === agent.id)) return

    agentsBySession.set(agent.session.id, agent)
    const dispose = agent.ctx.effect(() => {
      const offClaimed = agent.ctx.on('agent/inbox/claimed', (payload: ClaimedPayload) => {
        const message = payload?.message
        if (message === undefined) return
        recall.noteClaimed(agent.id, message)
        capture.noteClaimed(agent.id, message, typeof payload.turn === 'number' ? payload.turn : 0)
      })

      const offAssemble = agent.ctx.on(
        'system-prompt/assemble',
        async (assembly: { sections?: unknown[] }, _context: unknown, next: () => Promise<any>) => {
          const result = (await next()) ?? assembly
          if (!config.autoRecall && !config.coreAnchorEnabled) return result

          // The anchor describes the user, not the message: it is present on
          // every step so a compaction or a new session cannot erase it.
          let anchorItems: Array<{ content: string; memory_type: string }> = []
          if (config.coreAnchorEnabled) {
            try {
              anchorItems = await anchor.items()
            } catch {
              anchorItems = []
            }
          }

          let block: string | undefined
          if (config.autoRecall) {
            const pending = recall.contextFor(agent.id)
            if (pending !== undefined) {
              try {
                block = await pending
              } catch {
                block = undefined
              }
            }
          }

          const existing = Array.isArray(result?.sections) ? result.sections : []
          const sections = mergePromptSections(existing, [
            // The selection brief comes first: it is what the model reads before
            // deciding whether this turn deserves a memory.
            guide,
            buildAnchorSection(anchorItems, {
              maxChars: config.coreAnchorMaxChars,
              // Do not repeat a memory the per-turn recall already supplied.
              exclude: contextEntryKeys(block),
            }),
            buildContextSection(block),
          ])
          // Leave the assembly untouched when nothing was added or replaced, so
          // an idle step keeps the exact object the harness produced.
          if (sections.length === existing.length && sections.every((item, index) => item === existing[index])) {
            return result
          }
          return { ...result, sections }
        },
      )

      return () => {
        offClaimed()
        offAssemble()
      }
    })
    disposers.set(agent.id, dispose)
  }

  const teardownAgent = (agent: AgentLike | undefined): void => {
    if (agent === undefined) return
    const dispose = disposers.get(agent.id)
    disposers.delete(agent.id)
    agentsBySession.delete(agent.session.id)
    recall.clear(agent.id)
    capture.clear(agent.id)
    dispose?.()
  }

  ctx.effect(() => {
    const offCreated = ctx.on('agent/created', (payload: AgentPayload) => {
      if (payload?.agent !== undefined) setupAgent(payload.agent)
    })
    const offDisposed = ctx.on('agent/disposed', (payload: AgentPayload) => {
      teardownAgent(payload?.agent)
    })
    const offTurnEnd = ctx.on('session/event', (session: AgentSession, event: SessionEventLike) => {
      if (event?.type === 'turn/end') {
        const agent = agentsBySession.get(session?.id)
        if (agent !== undefined) {
          capture.turnEnded(agent.id, session.id, typeof event.data?.turn === 'number' ? event.data.turn : 0)
          recall.clear(agent.id)
        }
        return
      }
      if (event?.type === 'compaction/start') {
        // Verified against DSH 0.2.0-rc.2: `compaction/start` is a durable
        // observation event, appended before the summary is produced. It is
        // not cancellable, so this is a best-effort flush, never a gate.
        const agent = agentsBySession.get(session?.id)
        if (agent !== undefined) capture.compactionStarted(agent.id, session.id)
      }
    })

    return () => {
      offCreated()
      offDisposed()
      offTurnEnd()
      anchor.clear()
      for (const dispose of disposers.values()) dispose()
      disposers.clear()
      agentsBySession.clear()
    }
  })

  // HMR/reload: adopt agents that already exist when this plugin activates.
  for (const agent of ctx.agents.roots()) setupAgent(agent)

  // Tools are optional: a profile without the tool runtime still gets recall
  // and capture, and the tool fiber activates as soon as the service appears.
  ctx.inject(['tools'], (toolCtx: PluginContext) => {
    toolCtx.effect(() => {
      const toolDisposers = createJarvisMindTools(http, config, {
        // The model decided for this turn. The turn-end fallback must not then
        // submit the same turn as well; the queue records it per turn.
        onRemembered: (sessionId) => {
          if (sessionId === undefined) return
          const agent = agentsBySession.get(sessionId)
          if (agent !== undefined) capture.markRemembered(agent.id)
        },
      }).map((definition) => toolCtx.tools.register(definition))
      return () => {
        for (const dispose of toolDisposers) dispose()
      }
    })
  })

  ctx.logger.info(
    `jarvismind: ready (autoRecall=${config.autoRecall}, rememberMode=${config.rememberMode}, fallbackCapture=${config.fallbackCaptureOnTurnEnd}, memoryStyle=${config.memoryStyle}, baseUrl=${publicUrl(config.baseUrl)})`,
  )
  if (resolveToken(config) === undefined) {
    ctx.logger.warn(
      'jarvismind: no agent token found; recall, capture and tools will report "not configured" until one is provided',
    )
  }
}