/** Public configuration keys accepted from the Cordis row / user patch layer. */
export interface PluginConfig {
  /** JarvisMind server root, for example `http://127.0.0.1:8777`. */
  baseUrl?: string
  /** Fallback token file. `~` is expanded. */
  tokenFile?: string
  /** Environment variable read before the token file. */
  tokenEnv?: string
  /** Inject recalled memory into the system prompt before each model step. */
  autoRecall?: boolean
  /** Maximum number of memories requested for one recall. */
  recallLimit?: number
  /** Submit user turns to the governed remember endpoint after each turn. */
  autoRemember?: boolean
  /** Inject the always-on `<jarvismind-core-anchor>` soul/relationship block. */
  coreAnchorEnabled?: boolean
  /** Maximum number of memories kept in the core anchor. */
  coreAnchorLimit?: number
  /** Maximum characters kept per anchor memory. */
  coreAnchorMaxChars?: number
  /** Bound for one JarvisMind HTTP call, in milliseconds. */
  timeoutMs?: number
}

/** Configuration after defaults and bounds have been applied. */
export interface ResolvedConfig {
  baseUrl: string
  tokenFile: string
  tokenEnv: string
  autoRecall: boolean
  recallLimit: number
  autoRemember: boolean
  coreAnchorEnabled: boolean
  coreAnchorLimit: number
  coreAnchorMaxChars: number
  timeoutMs: number
}

/** One content block inside an agent message. */
export interface MessageContentBlock {
  type: string
  text?: string
  [key: string]: unknown
}

/** Attribution stored on every agent message by DSH. */
export interface MessageSource {
  kind?: string
  [key: string]: unknown
}

/** The subset of a DSH agent message this plugin reads. */
export interface AgentMessage {
  id: string
  role: string
  content: MessageContentBlock[]
  source?: MessageSource
}

/** The subset of a DSH Session this plugin reads. */
export interface AgentSession {
  id: string
}

/**
 * Minimal Cordis context surface used by the plugin.
 *
 * The real context is a `@deepseek-ai/cordis` Context; this structural type
 * keeps the plugin independent of Cordis' type package (which the Desktop
 * runtime resolves at execution time, not at build time).
 */
export interface AgentContextLike {
  on(event: string, listener: (...args: any[]) => any): () => void
  effect(setup: () => unknown): () => void
}

/** The subset of a live DSH Agent this plugin reads. */
export interface AgentLike {
  id: string
  session: AgentSession
  ctx: AgentContextLike
}

/** The subset of a tool execution context this plugin reads. */
export interface ToolExecLike {
  signal?: AbortSignal
  agent?: AgentLike
}

/** Logging surface shared by the plugin and its core services. */
export interface LoggerLike {
  info(...args: any[]): void
  warn(...args: any[]): void
  debug?(...args: any[]): void
}