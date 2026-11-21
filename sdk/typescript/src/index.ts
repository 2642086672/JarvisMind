/**
 * JarvisMind TypeScript SDK: a small client for the agent API (`/api/v1/agent`).
 *
 * Uses the global `fetch` (Node 18+ or a modern browser) and has no runtime
 * dependencies. Single-object methods return the `data` value of the response.
 * Paged list methods return the whole envelope (`data` plus `meta`).
 */

export const SPACE_HEADER = "X-JarvisMind-Space";
const USER_AGENT = "jarvismind-typescript-sdk/1.0.0";
const DEFAULT_TIMEOUT_MS = 30_000;

export interface ClientOptions {
  /** Server root, for example http://127.0.0.1:8777. */
  baseUrl: string;
  /** Agent token (jmt_...). Never logged or printed by the SDK. */
  token: string;
  /** Optional granted memory space. Omit to use the agent's default space. */
  spaceId?: string;
  timeoutMs?: number;
  /** Replace the transport, for example in tests. */
  fetchImpl?: typeof fetch;
}

export interface Envelope<T> {
  data: T[];
  meta: Record<string, unknown>;
}

export interface AgentIdentity {
  kind: string;
  label: string;
  agent_id: string;
  token_id: string;
  default_space_id: string;
  permissions: string[];
  spaces: Record<string, string>;
  active_space_id: string;
  accessible_spaces: { id: string; name: string; access: string }[];
}

export interface RememberResult {
  status: string;
  code: string;
  message: string;
  candidate_id: string | null;
  memory_id: string | null;
  content: string | null;
  memory_type: string | null;
  sensitive_categories: string[];
  merged_ids: string[];
  error_type: string | null;
}

export interface ContextResult {
  context: string;
  citations: { memory_id: string; memory_type: string; relevance_score: number }[];
  retrieval_version: string;
}

export interface RecallItem {
  memory_id: string;
  content: string;
  summary: string | null;
  memory_type: string;
  importance: number;
  relevance_score: number;
  updated_at: string;
  source: string;
  truncated: boolean;
}

export interface RecallResult {
  items: RecallItem[];
  total: number;
  retrieval_version: string;
  degraded_to_fts: boolean;
}

export interface MemorySummary {
  id: string;
  memory_type: string;
  content: string;
  summary: string | null;
  importance: number;
  status: string;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown> | null;
}

export interface ForgetRequestSummary {
  id: string;
  memory_id: string;
  status: string;
  requester_label: string;
  requested_at: string;
  reused_existing?: boolean;
}

export class JarvisMindError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  readonly retryAfterSeconds: number | undefined;

  constructor(message: string, status: number, code: string, details?: unknown, retryAfterSeconds?: number) {
    super(message);
    this.name = "JarvisMindError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

function errorFor(status: number, text: string, retryAfter: string | null): JarvisMindError {
  let code = "http_error";
  let message = `HTTP ${status}`;
  let details: unknown;
  try {
    const envelope = JSON.parse(text) as { error?: { code?: unknown; message?: unknown; details?: unknown } };
    if (envelope.error) {
      if (typeof envelope.error.code === "string") code = envelope.error.code;
      if (typeof envelope.error.message === "string") message = envelope.error.message;
      details = envelope.error.details;
    }
  } catch {
    // Non-JSON error bodies keep the generic message.
  }
  const seconds = retryAfter !== null && /^\d+$/.test(retryAfter) ? Number(retryAfter) : undefined;
  return new JarvisMindError(message, status, code, details, seconds);
}

export class JarvisMindClient {
  private readonly baseUrl: string;
  private readonly token: string;
  private readonly spaceId: string | undefined;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ClientOptions) {
    if (!/^https?:\/\//.test(options.baseUrl)) {
      throw new Error("baseUrl must start with http:// or https://");
    }
    if (!options.token) {
      throw new Error("an agent token is required");
    }
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.token = options.token;
    this.spaceId = options.spaceId;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch;
  }

  /** Never includes the token. */
  toString(): string {
    return `JarvisMindClient(${this.baseUrl}, space=${this.spaceId ?? "default"})`;
  }

  private async request<T>(
    method: "GET" | "POST" | "PUT" | "DELETE",
    path: string,
    options: { query?: Record<string, string | number | undefined>; body?: unknown } = {},
  ): Promise<T> {
    const url = new URL(`${this.baseUrl}${path}`);
    for (const [key, value] of Object.entries(options.query ?? {})) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.token}`,
      Accept: "application/json",
      "User-Agent": USER_AGENT,
    };
    if (this.spaceId) headers[SPACE_HEADER] = this.spaceId;
    let body: string | undefined;
    if (options.body !== undefined) {
      body = JSON.stringify(options.body);
      headers["Content-Type"] = "application/json";
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      response = await this.fetchImpl(url, { method, headers, body, signal: controller.signal });
    } catch {
      throw new JarvisMindError(`cannot reach JarvisMind at ${this.baseUrl}`, 0, "network_error");
    } finally {
      clearTimeout(timer);
    }
    const text = await response.text();
    if (!response.ok) {
      throw errorFor(response.status, text, response.headers.get("Retry-After"));
    }
    return (text ? JSON.parse(text) : {}) as T;
  }

  private static memoryPath(memoryId: string, suffix = ""): string {
    return `/api/v1/agent/memories/${encodeURIComponent(memoryId)}${suffix}`;
  }

  /** The agent's identity, permissions and usable spaces. */
  async me(): Promise<AgentIdentity> {
    return (await this.request<{ data: AgentIdentity }>("GET", "/api/v1/agent/me")).data;
  }

  /**
   * Submit one explicit memory through the governed path. `status` says what happened:
   * SAVED or UPDATED when stored; CONFLICT when it conflicts with an existing memory;
   * PROTECTED when sensitive and held (nothing is written); DUPLICATE, NO_MEMORY or NO_INTENT when not stored; FAILED when the write did not complete;
   * DENIED when it contains a credential. Credentials are never stored.
   */
  async remember(input: { text: string; conversationId: string; messageId?: string; turnId?: string; sourceId?: string }): Promise<RememberResult> {
    const body = {
      text: input.text,
      conversation_id: input.conversationId,
      message_id: input.messageId,
      turn_id: input.turnId,
      source_id: input.sourceId,
    };
    return (await this.request<{ data: RememberResult }>("POST", "/api/v1/agent/remember", { body })).data;
  }

  /** Extract memory candidates from a transcript. Candidates still pass through governance. */
  async extract(input: {
    conversation: string;
    conversationId: string;
    context?: string;
    messageId?: string;
    turnId?: string;
    sourceId?: string;
  }): Promise<unknown> {
    const body = {
      conversation: input.conversation,
      conversation_id: input.conversationId,
      context: input.context,
      message_id: input.messageId,
      turn_id: input.turnId,
      source_id: input.sourceId,
    };
    return (await this.request<{ data: unknown }>("POST", "/api/v1/agent/extract", { body })).data;
  }

  /**
   * Ask the owner to forget a memory. The memory is not changed by this call and the
   * response never contains a confirmation token. The owner decides in the console.
   */
  async forget(memoryId: string, reason?: string): Promise<ForgetRequestSummary> {
    const body = reason === undefined ? {} : { reason };
    return (
      await this.request<{ data: ForgetRequestSummary }>("POST", JarvisMindClient.memoryPath(memoryId, "/forget-requests"), {
        body,
      })
    ).data;
  }

  /**
   * Request a correction. Agents are non-human actors under JMR policy, so the server
   * refuses this with a PermissionDenied error (code `non_human_actor_forbidden`).
   */
  async update(memoryId: string, proposedChanges: Record<string, unknown>, reason?: string): Promise<unknown> {
    const body = { proposed_changes: proposedChanges, reason };
    return (
      await this.request<{ data: unknown }>("POST", JarvisMindClient.memoryPath(memoryId, "/modification-requests"), { body })
    ).data;
  }

  /** Ranked memories for a query, without context formatting. */
  async recall(query: string, options: { limit?: number } = {}): Promise<RecallResult> {
    return (
      await this.request<{ data: RecallResult }>("POST", "/api/v1/agent/recall", {
        body: { query, limit: options.limit ?? 8 },
      })
    ).data;
  }

  /** A context block for a system prompt, wrapped as untrusted data, plus citations. */
  async context(query: string, options: { limit?: number; maxContentChars?: number } = {}): Promise<ContextResult> {
    return (
      await this.request<{ data: ContextResult }>("POST", "/api/v1/agent/context", {
        body: { query, limit: options.limit ?? 8, max_content_chars: options.maxContentChars ?? 600 },
      })
    ).data;
  }

  /** Keyword search over the agent's readable memories. Returns the envelope. */
  async search(query: string, options: { limit?: number } = {}): Promise<Envelope<Record<string, unknown>>> {
    return this.request<Envelope<Record<string, unknown>>>("GET", "/api/v1/agent/search", {
      query: { q: query, limit: options.limit ?? 10 },
    });
  }

  /** Readable memories, newest first. Returns the envelope with paging metadata. */
  async listMemories(options: { status?: "ACTIVE" | "ARCHIVED"; page?: number; pageSize?: number } = {}): Promise<Envelope<MemorySummary>> {
    return this.request<Envelope<MemorySummary>>("GET", "/api/v1/agent/memories", {
      query: { status: options.status ?? "ACTIVE", page: options.page ?? 1, page_size: options.pageSize ?? 20 },
    });
  }

  /** One memory. Forgotten and merged memories are not returned. */
  async getMemory(memoryId: string): Promise<MemorySummary> {
    return (await this.request<{ data: MemorySummary }>("GET", JarvisMindClient.memoryPath(memoryId))).data;
  }

  /** Governance requests this agent created, with their current status. */
  async myRequests(options: { page?: number; pageSize?: number } = {}): Promise<Envelope<ForgetRequestSummary>> {
    return this.request<Envelope<ForgetRequestSummary>>("GET", "/api/v1/agent/governance/requests", {
      query: { page: options.page ?? 1, page_size: options.pageSize ?? 20 },
    });
  }

  /** This agent's own recent API access records (metadata only). */
  async events(options: { limit?: number } = {}): Promise<Envelope<Record<string, unknown>>> {
    return this.request<Envelope<Record<string, unknown>>>("GET", "/api/v1/agent/events", {
      query: { limit: options.limit ?? 50 },
    });
  }

  /** Whether the knowledge graph is enabled and usable for this space. */
  async graphStatus(): Promise<Record<string, unknown>> {
    return (await this.request<{ data: Record<string, unknown> }>("GET", "/api/v1/agent/graph/status")).data;
  }

  /** Graph entities whose source memory is still valid. Returns the envelope. */
  async graphNodes(options: { q?: string; page?: number; pageSize?: number } = {}): Promise<Envelope<Record<string, unknown>>> {
    return this.request<Envelope<Record<string, unknown>>>("GET", "/api/v1/agent/graph/nodes", {
      query: { q: options.q, page: options.page ?? 1, page_size: options.pageSize ?? 20 },
    });
  }
}
