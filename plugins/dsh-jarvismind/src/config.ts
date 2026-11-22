import type { PluginConfig, ResolvedConfig } from './types.js'

export const DEFAULT_BASE_URL = 'http://127.0.0.1:8777'
export const DEFAULT_TOKEN_FILE = '~/.jarvismind/dsh-agent.token'
export const DEFAULT_TOKEN_ENV = 'JARVISMIND_AGENT_TOKEN'
export const DEFAULT_RECALL_LIMIT = 6
export const DEFAULT_CORE_ANCHOR_LIMIT = 6
export const DEFAULT_CORE_ANCHOR_MAX_CHARS = 500
export const DEFAULT_TIMEOUT_MS = 2500

/** Who decides what is worth remembering. */
export const REMEMBER_MODES = ['agent', 'auto', 'both'] as const
export type RememberMode = 'agent' | 'auto' | 'both'
export const DEFAULT_REMEMBER_MODE: RememberMode = 'agent'

/** Which guidance the model receives about what to remember. */
export const MEMORY_STYLES = ['assistant', 'companion'] as const
export type MemoryStyle = 'assistant' | 'companion'
export const DEFAULT_MEMORY_STYLE: MemoryStyle = 'assistant'

/**
 * How long one core-anchor read is reused.
 *
 * The anchor describes a person, not a turn, so a 30 s window keeps it stable
 * across the steps of a turn at almost no request cost.
 */
export const CORE_ANCHOR_CACHE_TTL_MS = 30_000

/** The largest `page_size` the agent memory list endpoint accepts. */
export const CORE_ANCHOR_SCAN_LIMIT = 100

/** Keep an untrusted number inside a safe, documented range. */
function clampInteger(value: unknown, fallback: number, minimum: number, maximum: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback
  return Math.min(maximum, Math.max(minimum, Math.trunc(value)))
}

/** Accept only absolute http(s) roots; anything else falls back to loopback. */
function normalizeBaseUrl(value: unknown): string {
  if (typeof value !== 'string') return DEFAULT_BASE_URL
  try {
    const url = new URL(value.trim())
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return DEFAULT_BASE_URL
    // Drop credentials, query strings and fragments; keep an optional base path.
    return `${url.origin}${url.pathname.replace(/\/+$/, '')}`
  } catch {
    return DEFAULT_BASE_URL
  }
}

function nonEmptyString(value: unknown, fallback: string): string {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : fallback
}

/** Accept only a documented enum value; anything else falls back to the default. */
function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === 'string' && (allowed as readonly string[]).includes(value) ? (value as T) : fallback
}

/**
 * Apply defaults and bounds to the row config.
 *
 * Invalid values never disable the plugin: they fall back to the documented
 * defaults so a typo in `cordis.patch.yml` cannot break a chat session.
 */
export function normalizeConfig(input: PluginConfig | undefined): ResolvedConfig {
  const config = input ?? {}
  return {
    baseUrl: normalizeBaseUrl(config.baseUrl),
    tokenFile: nonEmptyString(config.tokenFile, DEFAULT_TOKEN_FILE),
    tokenEnv: nonEmptyString(config.tokenEnv, DEFAULT_TOKEN_ENV),
    autoRecall: config.autoRecall !== false,
    recallLimit: clampInteger(config.recallLimit, DEFAULT_RECALL_LIMIT, 1, 20),
    autoRemember: config.autoRemember !== false,
    rememberMode: oneOf(config.rememberMode, REMEMBER_MODES, DEFAULT_REMEMBER_MODE),
    memoryStyle: oneOf(config.memoryStyle, MEMORY_STYLES, DEFAULT_MEMORY_STYLE),
    coreAnchorEnabled: config.coreAnchorEnabled !== false,
    coreAnchorLimit: clampInteger(config.coreAnchorLimit, DEFAULT_CORE_ANCHOR_LIMIT, 1, 12),
    coreAnchorMaxChars: clampInteger(config.coreAnchorMaxChars, DEFAULT_CORE_ANCHOR_MAX_CHARS, 100, 2000),
    timeoutMs: clampInteger(config.timeoutMs, DEFAULT_TIMEOUT_MS, 200, 30_000),
  }
}