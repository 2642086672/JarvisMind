import { publicUrl, redactSecrets } from './security.js'

/** A JarvisMind failure with a stable code that never contains credentials. */
export class JarvisMindError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown

  constructor(message: string, status: number, code: string, details?: unknown) {
    super(redactSecrets(message))
    this.name = 'JarvisMindError'
    this.status = status
    this.code = code
    this.details = details
  }
}

export interface ContextCitation {
  memory_id: string
  memory_type: string
  relevance_score: number
}

export interface ContextResult {
  context: string
  citations: ContextCitation[]
  retrieval_version: string
}

export interface RememberResult {
  status: string
  code?: string
  message?: string
  candidate_id?: string | null
  memory_id?: string | null
  memory_type?: string | null
  [key: string]: unknown
}

export interface SearchHit {
  memory_id: string
  content: string
  summary?: string | null
  memory_type?: string
  importance?: number
  confidence?: number
  created_at?: string
  rank?: number
  [key: string]: unknown
}

/** One active memory as returned by the agent memory list endpoint. */
export interface AnchorMemory {
  id: string
  content: string
  memory_type: string
  importance?: number
  [key: string]: unknown
}

export interface ForgetResult {
  id: string
  memory_id: string
  status: string
  requester_label?: string
  reused_existing?: boolean
  [key: string]: unknown
}

interface JarvisMindHttpOptions {
  fetchImpl?: typeof fetch
  timeoutMs?: number
  userAgent?: string
}

const DEFAULT_TIMEOUT_MS = 2500
const USER_AGENT = 'dsh-plugin-jarvismind/0.1.0'

function errorFor(status: number, text: string): JarvisMindError {
  let code = 'http_error'
  let message = `JarvisMind returned HTTP ${status}`
  let details: unknown
  try {
    const envelope = JSON.parse(text) as { error?: { code?: unknown; message?: unknown; details?: unknown } }
    if (envelope.error !== undefined) {
      if (typeof envelope.error.code === 'string') code = envelope.error.code
      if (typeof envelope.error.message === 'string') message = envelope.error.message
      details = envelope.error.details
    }
  } catch {
    // Non-JSON bodies keep the generic HTTP message.
  }
  return new JarvisMindError(message, status, code, details)
}

/**
 * Thin, dependency-free client for the JarvisMind agent API.
 *
 * The client reads the token for every call, so rotating the file or the
 * environment variable takes effect without reloading the plugin.
 */
export class JarvisMindHttp {
  private readonly baseUrl: string
  private readonly tokenProvider: () => string | undefined
  private readonly fetchImpl: typeof fetch
  private readonly timeoutMs: number
  private readonly userAgent: string

  constructor(baseUrl: string, tokenProvider: () => string | undefined, options: JarvisMindHttpOptions = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, '')
    this.tokenProvider = tokenProvider
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch
    this.timeoutMs =
      typeof options.timeoutMs === 'number' && Number.isFinite(options.timeoutMs) && options.timeoutMs > 0
        ? options.timeoutMs
        : DEFAULT_TIMEOUT_MS
    this.userAgent = options.userAgent ?? USER_AGENT
  }

  /** Never includes the token or URL credentials. */
  toString(): string {
    return `JarvisMindHttp(${publicUrl(this.baseUrl)})`
  }

  private async request<T>(
    method: 'GET' | 'POST',
    path: string,
    options: { query?: Record<string, string | number>; body?: unknown; signal?: AbortSignal } = {},
  ): Promise<T> {
    const token = this.tokenProvider()
    if (token === undefined || token.length === 0) {
      throw new JarvisMindError('JarvisMind agent token is not configured', 0, 'not_configured')
    }

    const url = new URL(`${this.baseUrl}${path}`)
    for (const [key, value] of Object.entries(options.query ?? {})) {
      url.searchParams.set(key, String(value))
    }

    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
      Accept: 'application/json',
      'User-Agent': this.userAgent,
    }
    let body: string | undefined
    if (options.body !== undefined) {
      body = JSON.stringify(options.body)
      headers['Content-Type'] = 'application/json'
    }

    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), this.timeoutMs)
    const signal = options.signal === undefined ? controller.signal : AbortSignal.any([controller.signal, options.signal])

    try {
      let response: Response
      try {
        response = await this.fetchImpl(url, { method, headers, body, signal })
      } catch {
        throw this.transportFailure(options.signal, controller.signal)
      }

      let text: string
      try {
        text = await response.text()
      } catch {
        throw this.transportFailure(options.signal, controller.signal)
      }
      if (!response.ok) throw errorFor(response.status, text)
      if (text.length === 0) return {} as T
      try {
        return JSON.parse(text) as T
      } catch {
        throw new JarvisMindError('JarvisMind returned invalid JSON', response.status, 'invalid_response')
      }
    } finally {
      clearTimeout(timer)
    }
  }

  private transportFailure(external: AbortSignal | undefined, timeoutSignal: AbortSignal): JarvisMindError {
    if (external?.aborted === true) return new JarvisMindError('JarvisMind request was cancelled', 0, 'aborted')
    if (timeoutSignal.aborted) {
      return new JarvisMindError(`JarvisMind request timed out after ${this.timeoutMs} ms`, 0, 'timeout')
    }
    return new JarvisMindError(`cannot reach JarvisMind at ${publicUrl(this.baseUrl)}`, 0, 'network_error')
  }

  /** Retrieve the governed context block for one user message. */
  async context(query: string, limit: number, signal?: AbortSignal): Promise<ContextResult> {
    const envelope = await this.request<{ data: ContextResult }>('POST', '/api/v1/agent/context', {
      body: { query, limit, max_content_chars: 600 },
      ...(signal === undefined ? {} : { signal }),
    })
    const data = envelope.data
    return {
      context: typeof data?.context === 'string' ? data.context : '',
      citations: Array.isArray(data?.citations) ? data.citations : [],
      retrieval_version: typeof data?.retrieval_version === 'string' ? data.retrieval_version : 'unknown',
    }
  }

  /** Submit one user statement through the governed remember endpoint. */
  async remember(
    input: { text: string; conversationId: string; messageId?: string; turnId?: string; mode?: 'auto' | 'direct' },
    signal?: AbortSignal,
  ): Promise<RememberResult> {
    const body: Record<string, unknown> = {
      text: input.text,
      conversation_id: input.conversationId,
    }
    if (input.messageId !== undefined) body.message_id = input.messageId
    if (input.turnId !== undefined) body.turn_id = input.turnId
    // Omitted means the server default ("auto"), which keeps every existing
    // caller on its original behavior.
    if (input.mode !== undefined) body.mode = input.mode

    const envelope = await this.request<{ data: RememberResult }>('POST', '/api/v1/agent/remember', {
      body,
      ...(signal === undefined ? {} : { signal }),
    })
    return envelope.data
  }

  /** Keyword search over the agent's readable memories. */
  async search(query: string, limit: number, signal?: AbortSignal): Promise<SearchHit[]> {
    const envelope = await this.request<{ data: SearchHit[] }>('GET', '/api/v1/agent/search', {
      query: { q: query, limit },
      ...(signal === undefined ? {} : { signal }),
    })
    return Array.isArray(envelope.data) ? envelope.data : []
  }

  /**
   * List the agent's readable active memories.
   *
   * The core anchor needs the user's most defining memories, not matches for a
   * query, so it reads the documented list endpoint and ranks the result
   * locally. `page_size` is capped at 100 by the server.
   */
  async listMemories(limit: number, signal?: AbortSignal): Promise<AnchorMemory[]> {
    const envelope = await this.request<{ data: AnchorMemory[] }>('GET', '/api/v1/agent/memories', {
      query: { status: 'ACTIVE', page: 1, page_size: limit },
      ...(signal === undefined ? {} : { signal }),
    })
    return Array.isArray(envelope.data) ? envelope.data : []
  }

  /** Create a two-step forget request. The memory is unchanged until the owner approves. */
  async forget(memoryId: string, reason: string | undefined, signal?: AbortSignal): Promise<ForgetResult> {
    const path = `/api/v1/agent/memories/${encodeURIComponent(memoryId)}/forget-requests`
    const envelope = await this.request<{ data: ForgetResult }>('POST', path, {
      body: reason === undefined ? {} : { reason },
      ...(signal === undefined ? {} : { signal }),
    })
    return envelope.data
  }
}