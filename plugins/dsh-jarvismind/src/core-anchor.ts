import { CORE_ANCHOR_CACHE_TTL_MS, CORE_ANCHOR_SCAN_LIMIT } from './config.js'
import { JarvisMindError } from './http.js'
import type { AnchorMemory, JarvisMindHttp } from './http.js'
import type { LoggerLike, ResolvedConfig } from './types.js'

/**
 * Which memory types belong in a soul anchor, most defining first.
 *
 * `PROFILE` holds who the user is and how they want to be addressed, so it
 * outranks everything. `WORKING` is deliberately absent: it is scratch state
 * for the current task and must never become a permanent part of the prompt.
 */
const TYPE_PRIORITY: Record<string, number> = {
  PROFILE: 0,
  SEMANTIC: 1,
  EPISODIC: 2,
  REFLECTION: 3,
  PROJECT: 4,
}

/** Unknown types sort after every known one but are still eligible. */
const UNKNOWN_TYPE_PRIORITY = 5

/** Transient scratch state. Never part of a durable anchor. */
const EXCLUDED_TYPES = new Set(['WORKING'])

function importanceOf(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

/**
 * Pick the highest-value memories for the anchor.
 *
 * The agent API cannot sort by importance, so the ranking happens here: type
 * first, then importance, keeping the input order as the final tie-break so a
 * stable server response produces a stable prompt.
 */
export function rankAnchorMemories(rows: AnchorMemory[] | undefined, limit: number): AnchorMemory[] {
  if (!Array.isArray(rows)) return []
  const eligible = rows.filter(
    (row) =>
      row !== undefined &&
      typeof row.content === 'string' &&
      row.content.trim().length > 0 &&
      !EXCLUDED_TYPES.has(row.memory_type),
  )
  // Copy before sorting: the caller still owns its array.
  const ranked = eligible.map((row, index) => ({ row, index }))
  ranked.sort((a, b) => {
    const priorityA = TYPE_PRIORITY[a.row.memory_type] ?? UNKNOWN_TYPE_PRIORITY
    const priorityB = TYPE_PRIORITY[b.row.memory_type] ?? UNKNOWN_TYPE_PRIORITY
    if (priorityA !== priorityB) return priorityA - priorityB
    const importanceDelta = importanceOf(b.row.importance) - importanceOf(a.row.importance)
    if (importanceDelta !== 0) return importanceDelta
    return a.index - b.index
  })
  return ranked.slice(0, Math.max(0, limit)).map((entry) => entry.row)
}

interface CoreAnchorOptions {
  /** Clock injection so the TTL is testable without waiting. */
  now?: () => number
}

/**
 * One shared, short-lived read of the user's core memories.
 *
 * The anchor describes a person rather than a turn, so a 30 s window keeps the
 * prompt byte-stable across the steps of a turn while still picking up a new
 * memory quickly. The promise itself is cached, so concurrent assemblies share
 * one request instead of racing.
 *
 * Nothing here can throw at the caller: a missing token, a timeout, or a 5xx
 * resolves to an empty anchor and the chat continues.
 */
export class CoreAnchorCache {
  private cached: { expiresAt: number; promise: Promise<AnchorMemory[]> } | undefined
  private readonly now: () => number

  constructor(
    private readonly http: JarvisMindHttp,
    private readonly config: ResolvedConfig,
    private readonly logger: LoggerLike,
    options: CoreAnchorOptions = {},
  ) {
    this.now = options.now ?? Date.now
  }

  /** The ranked anchor memories, served from cache when it is still warm. */
  items(): Promise<AnchorMemory[]> {
    if (!this.config.coreAnchorEnabled) return Promise.resolve([])

    const at = this.now()
    const current = this.cached
    if (current !== undefined && at < current.expiresAt) return current.promise

    const promise = this.read()
    this.cached = { expiresAt: at + CORE_ANCHOR_CACHE_TTL_MS, promise }
    return promise
  }

  /** Drop the cached read. The plugin calls this on unload. */
  clear(): void {
    this.cached = undefined
  }

  private async read(): Promise<AnchorMemory[]> {
    try {
      const rows = await this.http.listMemories(CORE_ANCHOR_SCAN_LIMIT)
      return rankAnchorMemories(rows, this.config.coreAnchorLimit)
    } catch (error) {
      // Fail open. Log only the stable code: a server-supplied error message is
      // untrusted and this is the most sensitive read the plugin performs, so
      // its text must never reach a log line.
      const code = error instanceof JarvisMindError ? error.code : 'unexpected_error'
      this.logger.debug?.(`jarvismind: core anchor skipped (${code})`)
      return []
    }
  }
}
