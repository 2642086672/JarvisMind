import { apiRequest } from "./client";
import type {
  AccessLogItem,
  AccessSummary,
  ActivityItem,
  Agent,
  ApiManagement,
  ApiToken,
  AuditEntry,
  AutoRememberReceipt,
  BackupList,
  BackupVerification,
  Candidate,
  ComponentHealth,
  DashboardData,
  DataEnvelope,
  ForgetCreated,
  ForgetRequest,
  GovernanceOverview,
  GraphEdge,
  GraphNode,
  HealthResponse,
  IssuedToken,
  JkgListEnvelope,
  JkgNeighbor,
  JkgNodeDetail,
  JkgOverview,
  JkgStatus,
  JkgTraversal,
  LabContext,
  ListEnvelope,
  Memory,
  MemoryStatus,
  MemoryType,
  ModificationCreated,
  ModificationRequest,
  Permission,
  SearchHit,
  Space,
  SpaceDetail,
  SystemInfo,
} from "./types";

type Direction = "outgoing" | "incoming" | "both";

export type MemoryListQuery = {
  memory_type?: MemoryType;
  status?: MemoryStatus;
  page?: number;
  page_size?: number;
  sort_by?: "created_at" | "updated_at" | "importance" | "memory_type" | "status";
  sort_order?: "asc" | "desc";
};

export type MemorySearchQuery = {
  q: string;
  memory_type?: MemoryType;
  status?: MemoryStatus;
  page?: number;
  page_size?: number;
};

export type MemoryPatch = {
  content?: string;
  summary?: string | null;
  importance?: number;
  confidence?: number;
  status?: MemoryStatus;
  expires_at?: string | null;
};

export type ForgetListQuery = { status?: string; memory_id?: string; page?: number; page_size?: number };
export type ModificationListQuery = { status?: string; memory_id?: string; page?: number; page_size?: number };
export type CandidateListQuery = { status?: string; memory_type?: MemoryType; page?: number; page_size?: number };
export type GraphNodeQuery = { q?: string; entity_type?: string; status?: string; page?: number; page_size?: number };
export type GraphEdgeQuery = {
  node_id?: string;
  predicate?: string;
  direction?: Direction;
  valid_only?: boolean;
  page?: number;
  page_size?: number;
};
export type GraphNeighborQuery = {
  direction?: Direction;
  predicate?: string;
  valid_only?: boolean;
  page?: number;
  page_size?: number;
};
export type GraphTraverseQuery = {
  hops?: number;
  direction?: Direction;
  predicate?: string;
  valid_only?: boolean;
  max_nodes?: number;
  max_edges?: number;
};
export type AccessLogQuery = { limit?: number; offset?: number; agent_id?: string; status_family?: string };

export type SpaceCreateBody = { id: string; name: string; description?: string; space_type?: string };
export type SpaceUpdateBody = { name?: string; description?: string; status?: "active" | "archived" };
export type AgentCreateBody = {
  name: string;
  description?: string;
  agent_type?: string;
  source?: string;
  default_space_id?: string;
  permissions?: string[];
  grants?: { space_id: string; access: "read" | "readwrite" }[];
};
export type AgentUpdateBody = {
  name?: string;
  description?: string;
  agent_type?: string;
  status?: "active" | "disabled";
  permissions?: string[];
  default_space_id?: string;
};
export type TokenIssueBody = { name?: string; scopes?: string[]; expires_in_days?: number };

export type ApprovalBody = { confirmation_token?: string; actor_label?: string };
export type RejectionBody = { confirmation_token?: string; reason?: string };

/**
 * Every call the console makes. Paths mirror the server routes one to one. Nothing
 * here is cached: a page reloads its data when its selection changes.
 */
export const api = {
  health: () => apiRequest<HealthResponse>("/health", { auth: false }),
  systemInfo: () => apiRequest<SystemInfo>("/api/v1/system/info"),
  validateOwnerToken: (token: string) => apiRequest<unknown>("/api/v1/console/settings", { token, space: false }),

  listMemories: (query: MemoryListQuery) => apiRequest<ListEnvelope<Memory>>("/api/v1/memories", { query }),
  searchMemories: (query: MemorySearchQuery) =>
    apiRequest<ListEnvelope<SearchHit>>("/api/v1/memories/search", { query }),
  getMemory: (id: string) => apiRequest<DataEnvelope<Memory>>(`/api/v1/memories/${encodeURIComponent(id)}`),
  memoryAudit: (id: string, page = 1, pageSize = 50) =>
    apiRequest<ListEnvelope<AuditEntry>>(`/api/v1/memories/${encodeURIComponent(id)}/audit-logs`, {
      query: { page, page_size: pageSize },
    }),
  autoRemember: (body: { text: string; conversation_id: string }) =>
    apiRequest<DataEnvelope<AutoRememberReceipt>>("/api/v1/memories/auto-remember", { method: "POST", body }),
  patchMemory: (id: string, body: MemoryPatch) =>
    apiRequest<DataEnvelope<Memory>>(`/api/v1/memories/${encodeURIComponent(id)}`, { method: "PATCH", body }),

  createForgetRequest: (memoryId: string, body: { reason?: string }) =>
    apiRequest<DataEnvelope<ForgetCreated>>(`/api/v1/memories/${encodeURIComponent(memoryId)}/forget-requests`, {
      method: "POST",
      body,
    }),
  listForgetRequests: (query: ForgetListQuery) =>
    apiRequest<ListEnvelope<ForgetRequest>>("/api/v1/forget-requests", { query }),
  approveForgetRequest: (id: string, body: ApprovalBody) =>
    apiRequest<DataEnvelope<ForgetRequest>>(`/api/v1/forget-requests/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      body,
    }),
  rejectForgetRequest: (id: string, body: RejectionBody) =>
    apiRequest<DataEnvelope<ForgetRequest>>(`/api/v1/forget-requests/${encodeURIComponent(id)}/reject`, {
      method: "POST",
      body,
    }),

  createModificationRequest: (
    memoryId: string,
    body: { proposed_changes?: Record<string, unknown>; request_kind?: string; reason?: string },
  ) =>
    apiRequest<DataEnvelope<ModificationCreated>>(
      `/api/v1/memories/${encodeURIComponent(memoryId)}/modification-requests`,
      { method: "POST", body },
    ),
  listModificationRequests: (query: ModificationListQuery) =>
    apiRequest<ListEnvelope<ModificationRequest>>("/api/v1/modification-requests", { query }),
  approveModificationRequest: (id: string, body: { confirmation_token: string; actor_label?: string }) =>
    apiRequest<DataEnvelope<ModificationRequest>>(
      `/api/v1/modification-requests/${encodeURIComponent(id)}/approve`,
      { method: "POST", body },
    ),
  rejectModificationRequest: (id: string, body: { reason?: string }) =>
    apiRequest<DataEnvelope<ModificationRequest>>(
      `/api/v1/modification-requests/${encodeURIComponent(id)}/reject`,
      { method: "POST", body },
    ),

  listCandidates: (query: CandidateListQuery) =>
    apiRequest<ListEnvelope<Candidate>>("/api/v1/memories/candidates", { query }),
  approveCandidate: (id: string, body: { reason?: string }) =>
    apiRequest<DataEnvelope<Candidate>>(`/api/v1/memories/candidates/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      body,
    }),
  rejectCandidate: (id: string, body: { reason?: string }) =>
    apiRequest<DataEnvelope<Candidate>>(`/api/v1/memories/candidates/${encodeURIComponent(id)}/reject`, {
      method: "POST",
      body,
    }),

  labContext: (body: { query: string; limit?: number; max_content_chars?: number }) =>
    apiRequest<DataEnvelope<LabContext>>("/api/v1/console/lab/context", { method: "POST", body }),

  graphStatus: () => apiRequest<JkgStatus>("/api/v1/jkg/status"),
  graphOverview: () => apiRequest<JkgOverview>("/api/v1/jkg/overview"),
  graphNodes: (query: GraphNodeQuery) => apiRequest<JkgListEnvelope<GraphNode>>("/api/v1/jkg/nodes", { query }),
  graphNode: (id: string) => apiRequest<DataEnvelope<JkgNodeDetail>>(`/api/v1/jkg/nodes/${encodeURIComponent(id)}`),
  graphNeighbors: (id: string, query: GraphNeighborQuery) =>
    apiRequest<JkgListEnvelope<JkgNeighbor>>(`/api/v1/jkg/nodes/${encodeURIComponent(id)}/neighbors`, { query }),
  graphTraverse: (id: string, query: GraphTraverseQuery) =>
    apiRequest<DataEnvelope<JkgTraversal>>(`/api/v1/jkg/nodes/${encodeURIComponent(id)}/traverse`, { query }),
  graphEdges: (query: GraphEdgeQuery) => apiRequest<JkgListEnvelope<GraphEdge>>("/api/v1/jkg/edges", { query }),
  graphEdge: (id: string) => apiRequest<DataEnvelope<GraphEdge>>(`/api/v1/jkg/edges/${encodeURIComponent(id)}`),
  graphSyncStatus: () => apiRequest<Record<string, unknown>>("/api/v1/jkg/sync/status"),
  graphWorkerStatus: () => apiRequest<Record<string, unknown>>("/api/v1/jkg/worker/status"),

  listSpaces: (includeArchived = false) =>
    apiRequest<DataEnvelope<Space[]>>("/api/v1/control/spaces", {
      query: { include_archived: includeArchived },
    }),
  getSpace: (id: string) => apiRequest<DataEnvelope<SpaceDetail>>(`/api/v1/control/spaces/${encodeURIComponent(id)}`),
  createSpace: (body: SpaceCreateBody) =>
    apiRequest<DataEnvelope<Space & { schema_version: number }>>("/api/v1/control/spaces", {
      method: "POST",
      body,
    }),
  updateSpace: (id: string, body: SpaceUpdateBody) =>
    apiRequest<DataEnvelope<Space>>(`/api/v1/control/spaces/${encodeURIComponent(id)}`, { method: "PATCH", body }),

  listAgents: () => apiRequest<DataEnvelope<Agent[]>>("/api/v1/control/agents"),
  getAgent: (id: string) => apiRequest<DataEnvelope<Agent>>(`/api/v1/control/agents/${encodeURIComponent(id)}`),
  createAgent: (body: AgentCreateBody) =>
    apiRequest<DataEnvelope<Agent>>("/api/v1/control/agents", { method: "POST", body }),
  updateAgent: (id: string, body: AgentUpdateBody) =>
    apiRequest<DataEnvelope<Agent>>(`/api/v1/control/agents/${encodeURIComponent(id)}`, { method: "PATCH", body }),
  deleteAgent: (id: string) =>
    apiRequest<DataEnvelope<{ id: string; status: string }>>(`/api/v1/control/agents/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  setGrant: (agentId: string, spaceId: string, access: "read" | "readwrite") =>
    apiRequest<DataEnvelope<Agent>>(
      `/api/v1/control/agents/${encodeURIComponent(agentId)}/grants/${encodeURIComponent(spaceId)}`,
      { method: "PUT", body: { access } },
    ),
  revokeGrant: (agentId: string, spaceId: string) =>
    apiRequest<DataEnvelope<Agent>>(
      `/api/v1/control/agents/${encodeURIComponent(agentId)}/grants/${encodeURIComponent(spaceId)}`,
      { method: "DELETE" },
    ),
  permissions: () => apiRequest<{ data: Permission[] }>("/api/v1/control/permissions"),
  listTokens: (agentId?: string) =>
    apiRequest<DataEnvelope<ApiToken[]>>("/api/v1/control/tokens", { query: { agent_id: agentId } }),
  issueToken: (agentId: string, body: TokenIssueBody) =>
    apiRequest<IssuedToken>(`/api/v1/control/agents/${encodeURIComponent(agentId)}/tokens`, {
      method: "POST",
      body,
    }),
  revokeToken: (id: string) =>
    apiRequest<DataEnvelope<ApiToken>>(`/api/v1/control/tokens/${encodeURIComponent(id)}/revoke`, { method: "POST" }),
  rotateToken: (id: string) =>
    apiRequest<IssuedToken>(`/api/v1/control/tokens/${encodeURIComponent(id)}/rotate`, { method: "POST" }),
  accessLogs: (query: AccessLogQuery) =>
    apiRequest<{ data: AccessLogItem[]; meta: { total: number; limit: number; offset: number } }>(
      "/api/v1/control/access-logs",
      { query },
    ),
  accessSummary: (hours = 24) =>
    apiRequest<DataEnvelope<AccessSummary>>("/api/v1/control/access-logs/summary", { query: { hours } }),

  dashboard: () => apiRequest<DataEnvelope<DashboardData>>("/api/v1/console/dashboard"),
  activity: (limit = 50) => apiRequest<DataEnvelope<ActivityItem[]>>("/api/v1/console/activity", { query: { limit } }),
  opsComponents: () => apiRequest<DataEnvelope<ComponentHealth[]>>("/api/v1/console/ops/components"),
  opsTasks: () => apiRequest<DataEnvelope<Record<string, unknown>>>("/api/v1/console/ops/tasks"),
  opsLogs: (query: AccessLogQuery) =>
    apiRequest<{ data: AccessLogItem[]; meta: { total: number; limit: number; offset: number } }>(
      "/api/v1/console/ops/logs",
      { query },
    ),
  jmrStatus: () => apiRequest<Record<string, unknown>>("/api/v1/jmr/status"),
  governanceOverview: () => apiRequest<DataEnvelope<GovernanceOverview>>("/api/v1/console/governance/overview"),
  apiManagement: () => apiRequest<DataEnvelope<ApiManagement>>("/api/v1/console/api-management"),
  settings: () => apiRequest<DataEnvelope<Record<string, unknown>>>("/api/v1/console/settings"),
  listBackups: () => apiRequest<BackupList>("/api/v1/console/backups"),
  createBackup: () => apiRequest<DataEnvelope<{ name: string; size_bytes: number }>>("/api/v1/console/backups", { method: "POST" }),
  verifyBackup: (name: string) =>
    apiRequest<DataEnvelope<BackupVerification>>("/api/v1/console/backups/verify", { method: "POST", body: { name } }),
};
