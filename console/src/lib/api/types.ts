/**
 * Response shapes of the JarvisMind API as the console consumes them.
 *
 * The console displays what the server returns. It never derives authority from
 * these types: every permission decision is made by the API.
 */

export interface PageMeta {
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

export interface ListEnvelope<T> {
  data: T[];
  meta: PageMeta;
}

export interface DataEnvelope<T> {
  data: T;
}

export type MemoryType = "PROFILE" | "SEMANTIC" | "EPISODIC" | "PROJECT" | "WORKING" | "REFLECTION";
export type MemoryStatus = "ACTIVE" | "ARCHIVED" | "DELETED" | "MERGED";

export const MEMORY_TYPES: readonly MemoryType[] = [
  "PROFILE",
  "SEMANTIC",
  "EPISODIC",
  "PROJECT",
  "WORKING",
  "REFLECTION",
];

export interface Memory {
  id: string;
  memory_type: MemoryType;
  content: string;
  summary: string | null;
  importance: number;
  confidence: number;
  status: MemoryStatus;
  source_id: string | null;
  created_at: string;
  updated_at: string;
  accessed_at: string | null;
  expires_at: string | null;
  metadata: Record<string, unknown> | null;
  forgotten_at: string | null;
  forget_reason: string | null;
  merged_into: string | null;
}

export interface AuditEntry {
  action: string;
  actor: string | null;
  detail: Record<string, unknown> | null;
  created_at: string;
}

export interface SearchHit {
  memory_id: string;
  content: string;
  summary: string | null;
  memory_type: MemoryType;
  importance: number;
  confidence: number;
  created_at: string;
  rank: number | null;
}

export type ForgetStatus = "PENDING" | "APPROVED" | "EXECUTED" | "REJECTED" | "FAILED" | (string & {});

export interface ForgetRequest {
  id: string;
  memory_id: string;
  status: ForgetStatus;
  request_source: string;
  requester_label: string;
  reason: string | null;
  requested_at: string;
  confirmation_expires_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decision_source: string | null;
  executed_at: string | null;
  execution_result: string | null;
  failure_reason: string | null;
  updated_at: string;
  awaiting_user_decision: boolean;
  memory_forgotten: boolean;
}

/** Returned once, at creation. The token is kept only in memory by the console. */
export interface ForgetCreated extends ForgetRequest {
  confirmation_token: string;
  reused_existing: boolean;
}

export interface ModificationRequest {
  id: string;
  memory_id: string;
  status: string;
  request_kind: string;
  requester_label?: string;
  reason?: string | null;
  created_at?: string;
  requested_at?: string;
  [field: string]: unknown;
}

export interface ModificationCreated {
  request: ModificationRequest;
  /** Returned once, at creation. The console keeps it only in memory. */
  confirmation_token: string;
  confirmation_expires_in_seconds: number;
  reused_pending_request: boolean;
  authentication_level: string;
  authorization_verification_result: string;
}

export interface AutoRememberReceipt {
  status: string;
  code: string;
  message: string;
  candidate_id: string | null;
  memory_id: string | null;
  content: string | null;
  memory_type: MemoryType | null;
  sensitive_categories: string[];
  merged_ids: string[];
  error_type: string | null;
}

export interface Candidate {
  id: string;
  conversation_id: string | null;
  memory_type: MemoryType;
  content: string;
  summary: string | null;
  importance: number;
  confidence: number;
  status: "PENDING" | "PENDING_AUTHORIZATION" | "APPROVED" | "REJECTED" | (string & {});
  sensitive_categories: string[];
  extracted_at: string;
  decided_at: string | null;
  decided_by: string | null;
  reject_reason: string | null;
  scan_status: string | null;
}

export interface GraphPageMeta {
  page: number;
  page_size: number;
  total: number | null;
  total_pages?: number;
  truncated?: boolean;
  scanned?: number;
  candidate_total?: number | null;
}

export interface JkgListEnvelope<T> {
  data: T[];
  meta: GraphPageMeta;
}

export interface GraphNode {
  node_id: string;
  entity_type: string;
  name: string;
  display_name: string;
  canonical_name: string;
  normalized_key: string;
  source_memory_id: string;
  status: string;
  generation: number;
  created_at: string | null;
  updated_at: string | null;
  source_memory_status: string | null;
  source_memory_valid: boolean;
  sensitivity: string;
  name_redacted: boolean;
  metadata_redacted: boolean;
}

export interface GraphSource {
  memory_id?: string;
  memory_status: string | null;
  created_at: string | null;
  invalidated_at: string | null;
  invalidation_reason: string | null;
  is_valid: boolean;
}

export interface GraphEdge {
  edge_id: string;
  predicate: string;
  relation_type: string;
  subject_id: string;
  object_id: string;
  status: string;
  generation: number;
  confidence: number;
  recorded_at: string | null;
  updated_at: string | null;
  source_count: number;
  valid_source_count: number;
  is_valid: boolean;
  sensitivity: string;
  metadata_redacted: boolean;
  sources?: GraphSource[];
}

export interface JkgStatus {
  enabled: boolean;
  enabled_source: string;
  state: string;
  usable: boolean;
  schema_ready: boolean;
  missing_tables: string[];
  present_tables: string[];
  schema_version_expected: number;
  page_size: number;
  max_page_size: number;
  max_scan: number;
}

export interface JkgOverview {
  counts: { nodes: number; edges: number; sources: number; invalidated_sources: number };
  scanned_edges: number;
  valid_edges: number;
  invalid_edges: number;
  truncated: boolean;
  orphan_edges: { edge_id: string; predicate: string; subject_id: string; object_id: string; source_count: number }[];
}

export interface JkgNodeDetail {
  node: GraphNode;
  outgoing_edges: GraphEdge[];
  incoming_edges: GraphEdge[];
  sources: { memory_id: string; memory_status: string | null; is_valid: boolean }[];
  edge_total: number | null;
  edges_truncated: boolean;
}

export interface JkgNeighbor {
  node: GraphNode;
  edge: GraphEdge;
  direction: "outgoing" | "incoming";
}

export interface JkgTraversal {
  root_id: string;
  hops: number;
  direction: string;
  nodes: { node: GraphNode; hop: number }[];
  edges: GraphEdge[];
  truncated: boolean;
  visited_nodes: number;
  visited_edges: number;
  limits: { hops: number; max_nodes: number; max_edges: number };
}

export interface Space {
  id: string;
  name: string;
  description: string;
  space_type: string;
  status: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface SpaceDetail extends Space {
  provisioned: boolean;
  memory_counts: Record<string, number> | null;
  agents: { id: string; name: string; access: string }[];
}

export interface AgentGrant {
  space_id: string;
  access: "read" | "readwrite";
}

export interface Agent {
  id: string;
  name: string;
  description: string;
  agent_type: string;
  source: string;
  status: string;
  default_space_id: string;
  permissions: string[];
  grants: AgentGrant[];
  active_token_count: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface ApiToken {
  id: string;
  agent_id: string;
  name: string;
  token_prefix: string;
  status: string;
  scopes: string[];
  expires_at: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  replaced_by: string | null;
}

export interface IssuedToken {
  data: ApiToken;
  /** The plaintext token. Shown once; never stored by the console. */
  token: string;
  notice: string;
}

export interface Permission {
  name: string;
  description: string;
}

export interface AccessLogItem {
  id: number;
  occurred_at: string;
  principal_kind: string;
  agent_id: string | null;
  space_id: string | null;
  method: string;
  route: string;
  status_code: number;
  latency_ms: number;
  error_code: string | null;
}

export interface AccessSummary {
  window_hours: number;
  total: number;
  by_status_family: Record<string, number>;
  average_latency_ms: number;
  top_error_codes: { code: string; count: number }[];
  top_routes: { route: string; count: number }[];
}

export interface ComponentHealth {
  component: string;
  status: string;
  detail?: unknown;
  seconds?: number;
  version?: string;
}

export type ActivityItem =
  | { type: "memory"; action: string; actor: string | null; memory_id: string | null; at: string }
  | { type: "api"; action: string; status_code: number; principal: string; agent_id: string | null; at: string };

export interface DashboardData {
  memory: {
    total: number;
    by_status: Record<string, number>;
    active: number;
    archived: number;
    deleted: number;
    today_created: number;
    today_updated: number;
    pending_governance: number;
  };
  pending: {
    candidates: number;
    sensitive_authorization: number;
    forget_requests: number;
    modification_requests: number;
    rejected_candidates: number;
  };
  growth: { day: string; count: number }[];
  distribution: {
    types: { type: string; count: number }[];
    sources: { source: string; count: number }[];
  };
  graph: {
    status: string;
    health?: JkgStatus;
    counts?: JkgOverview["counts"];
    invalid_edges?: number;
    orphan_edges?: number;
    scan_truncated?: boolean;
  };
  health: ComponentHealth[];
  activity: ActivityItem[];
  not_available: string[];
}

export interface GovernanceOverview {
  pending: DashboardData["pending"];
  policies: {
    credential_gate: string;
    sensitive_facts: string;
    auto_save_enabled: boolean;
    direct_write_enabled: boolean;
    p1_governance_enabled: boolean;
    lifecycle: { import: boolean; merge: boolean; summary_review: boolean; correction_resolve: boolean };
    non_human_actors: string;
  };
  memory_types: string[];
  recent_audit: { action: string; actor: string | null; memory_id: string | null; at: string }[];
}

export interface ApiManagement {
  version: string;
  endpoints: { method: string; path: string; summary: string; tags: string[] }[];
  usage: AccessSummary;
  rate_limits: {
    owner_per_minute: number;
    agent_per_minute: number;
    auth_failures_per_minute: number;
    window_seconds: number;
    scope: string;
  };
  authentication: Record<string, string>;
  error_envelope: Record<string, unknown>;
}

export interface BackupItem {
  name: string;
  size_bytes: number;
  modified_at: string;
}

export interface BackupList {
  data: BackupItem[];
  restore: { available_via_api: boolean; procedure: string };
}

export interface BackupVerification {
  name: string;
  ok: boolean;
  integrity?: string;
  memory_records?: number;
  reason?: string;
}

export interface ContextItem {
  memory_id: string;
  content: string;
  summary: string | null;
  memory_type: MemoryType;
  importance: number;
  relevance_score: number;
  updated_at: string;
  source: string;
  truncated: boolean;
}

export interface ContextExcluded {
  expired: number;
  sensitive: number;
  irrelevant: number;
  duplicate: number;
  filtered: number;
}

export interface LabContext {
  context: string;
  items: ContextItem[];
  total: number;
  retrieval_version: string;
  degraded_to_fts: boolean;
  excluded: ContextExcluded;
}

export interface HealthResponse {
  status: string;
  version: string;
  database_connected: boolean;
  schema_version: number;
}

export interface SystemInfo {
  name: string;
  version: string;
  python_version: string;
  database_type: string;
  data_dir: string;
  database_file: string;
  memory_types: string[];
  statuses: string[];
  status: string;
  uptime_seconds: number;
}
