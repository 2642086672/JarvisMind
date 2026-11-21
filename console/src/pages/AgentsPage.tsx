import { useState, type FormEvent } from "react";
import { useConsole } from "../app/ConsoleContext";
import { describeError } from "../lib/api/client";
import { api, type AgentCreateBody } from "../lib/api/endpoints";
import type { AccessLogItem, Agent, ApiToken, DataEnvelope, IssuedToken, Permission } from "../lib/api/types";
import { useAsync } from "../lib/hooks";
import { formatDateTime, statusTone } from "../lib/format";
import {
  Badge,
  Button,
  Card,
  Column,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  Modal,
  Notice,
  PageHeader,
  useToast,
} from "../components/ui";

const AGENT_TYPES: { id: string; label: string }[] = [
  { id: "dsh", label: "DSH" },
  { id: "deepseek", label: "DeepSeek" },
  { id: "openai_compatible", label: "OpenAI 兼容接口" },
  { id: "claude", label: "Claude" },
  { id: "openclaw", label: "OpenClaw" },
  { id: "langchain", label: "LangChain" },
  { id: "llamaindex", label: "LlamaIndex" },
  { id: "custom", label: "自定义" },
];

const DEFAULT_PERMISSIONS = ["read", "search", "context"];
const ACCESS_LABELS: Record<string, string> = { read: "只读", readwrite: "读写" };

interface AgentDraft {
  name: string;
  description: string;
  agent_type: string;
  default_space_id: string;
  permissions: string[];
}

interface LogPage {
  data: AccessLogItem[];
  meta: { total: number; limit: number; offset: number };
}

function emptyDraft(defaultSpace: string): AgentDraft {
  return {
    name: "",
    description: "",
    agent_type: "dsh",
    default_space_id: defaultSpace,
    permissions: DEFAULT_PERMISSIONS,
  };
}

function toggle(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

export function AgentsPage() {
  const { spaceId, spacesVersion, timezone } = useConsole();
  const toast = useToast();
  const agents = useAsync(() => api.listAgents(), [spaceId, spacesVersion]);
  const permissions = useAsync(() => api.permissions(), []);
  const spaces = useAsync(() => api.listSpaces(false), [spacesVersion]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [draft, setDraft] = useState<AgentDraft>(emptyDraft("default"));
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editPermissions, setEditPermissions] = useState<string[] | null>(null);
  const [grantSpace, setGrantSpace] = useState("");
  const [grantAccess, setGrantAccess] = useState<"read" | "readwrite">("read");
  const [tokenName, setTokenName] = useState("primary");
  const [tokenDays, setTokenDays] = useState(90);
  const [tokenScopes, setTokenScopes] = useState<string[] | null>(null);
  const [issued, setIssued] = useState<IssuedToken | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const agentList: Agent[] = agents.data?.data ?? [];
  const selected = agentList.find((agent) => agent.id === selectedId) ?? null;
  const permissionList: Permission[] = permissions.data?.data ?? [];

  const tokens = useAsync<DataEnvelope<ApiToken[]>>(
    () => (selectedId ? api.listTokens(selectedId) : Promise.resolve({ data: [] as ApiToken[] })),
    [selectedId, spacesVersion],
  );
  const logs = useAsync<LogPage>(
    () =>
      selectedId
        ? api.accessLogs({ agent_id: selectedId, limit: 30 })
        : Promise.resolve({ data: [] as AccessLogItem[], meta: { total: 0, limit: 30, offset: 0 } }),
    [selectedId],
  );

  function resetEditors(): void {
    setEditPermissions(null);
    setGrantSpace("");
    setTokenScopes(null);
    setError(null);
  }

  async function run(successMessage: string, action: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await action();
      toast.notify(successMessage, "success");
      agents.reload();
      tokens.reload();
      logs.reload();
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  async function createAgent(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const name = draft.name.trim();
    if (name.length === 0) {
      setFormError("请填写 Agent 名称。");
      return;
    }
    if (draft.permissions.length === 0) {
      setFormError("至少选择一项权限。");
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      const body: AgentCreateBody = {
        name,
        description: draft.description.trim(),
        agent_type: draft.agent_type,
        default_space_id: draft.default_space_id,
        permissions: draft.permissions,
      };
      const response = await api.createAgent(body);
      toast.notify(`已创建 Agent：${response.data.name}。请为它颁发令牌。`, "success");
      setCreateOpen(false);
      setDraft(emptyDraft(spaceId));
      setSelectedId(response.data.id);
      resetEditors();
      agents.reload();
    } catch (cause) {
      setFormError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  async function issueToken(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!selected) return;
    const scopes = tokenScopes ?? selected.permissions;
    if (scopes.length === 0) {
      setError("令牌至少需要一项权限。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await api.issueToken(selected.id, {
        name: tokenName.trim() || "default",
        scopes,
        expires_in_days: tokenDays,
      });
      setIssued(response);
      setTokenScopes(null);
      tokens.reload();
      agents.reload();
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  async function rotate(token: ApiToken): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const response = await api.rotateToken(token.id);
      setIssued(response);
      tokens.reload();
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  const tokenColumns: Column<ApiToken>[] = [
    { key: "name", header: "名称", render: (row) => `${row.name}（${row.token_prefix}…）` },
    { key: "scopes", header: "权限", render: (row) => row.scopes.join("、") },
    { key: "status", header: "状态", render: (row) => <Badge tone={statusTone(row.status)}>{row.status}</Badge> },
    { key: "expires", header: "到期", render: (row) => formatDateTime(row.expires_at, timezone) },
    { key: "used", header: "最近使用", render: (row) => formatDateTime(row.last_used_at, timezone) },
    {
      key: "actions",
      header: "操作",
      render: (row) =>
        row.status === "active" ? (
          <div className="toolbar">
            <Button size="sm" disabled={busy} onClick={() => void rotate(row)}>
              轮换
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={busy}
              onClick={() => void run("令牌已撤销", () => api.revokeToken(row.id))}
            >
              撤销
            </Button>
          </div>
        ) : (
          <span className="muted small">—</span>
        ),
    },
  ];

  const spaceOptions = spaces.data?.data ?? [];
  const currentPermissions = editPermissions ?? selected?.permissions ?? [];
  const tokenScopeChoices = selected?.permissions ?? [];
  const activeScopes = tokenScopes ?? tokenScopeChoices;

  return (
    <>
      <PageHeader
        title="Agent 管理"
        description="为每个接入的 AI 应用创建 Agent，分配空间授权、细粒度权限与令牌。权限由服务端强制执行，界面隐藏按钮不构成授权。"
        actions={
          <Button variant="primary" onClick={() => setCreateOpen(true)}>
            新建 Agent
          </Button>
        }
      />
      <Notice tone="info" title="令牌安全">
        令牌明文只在创建或轮换时显示一次，服务端只保存 HMAC 摘要。丢失的令牌无法找回，只能撤销后重新颁发。
      </Notice>
      {error ? <Notice tone="danger">{error}</Notice> : null}

      <div className="grid two">
        <Card title="Agent 列表" subtitle={`共 ${agentList.length} 个（已删除的不显示）`}>
          {agents.error ? (
            <ErrorState error={agents.error} onRetry={agents.reload} />
          ) : (
            <DataTable
              caption="Agent 列表"
              rows={agentList}
              rowKey={(row) => row.id}
              onRowClick={(row) => {
                setSelectedId(row.id);
                resetEditors();
              }}
              empty={<EmptyState title="还没有 Agent" description="点击右上角“新建 Agent”，为 DSH 或其它 AI 应用创建身份。" />}
              columns={[
                {
                  key: "name",
                  header: "名称",
                  render: (row) => (
                    <>
                      {row.name} <Badge tone={statusTone(row.status)}>{row.status === "active" ? "启用" : "已停用"}</Badge>
                      <div className="muted small">{AGENT_TYPES.find((item) => item.id === row.agent_type)?.label ?? row.agent_type}</div>
                    </>
                  ),
                },
                { key: "space", header: "默认空间", render: (row) => <code>{row.default_space_id}</code> },
                { key: "tokens", header: "有效令牌", render: (row) => String(row.active_token_count) },
              ]}
            />
          )}
        </Card>

        <Card title={selected ? selected.name : "Agent 详情"} subtitle={selected ? `ID：${selected.id}` : "在左侧选择一个 Agent"}>
          {!selected ? (
            <EmptyState title="未选择 Agent" description="选择 Agent 后，可以管理它的权限、空间授权、令牌与访问记录。" />
          ) : (
            <div className="stack">
              <div className="toolbar wrap">
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    void run(selected.status === "active" ? "Agent 已停用，其令牌立即失效" : "Agent 已启用", () =>
                      api.updateAgent(selected.id, { status: selected.status === "active" ? "disabled" : "active" }),
                    )
                  }
                >
                  {selected.status === "active" ? "停用" : "启用"}
                </Button>
                <Button size="sm" variant="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
                  删除 Agent
                </Button>
              </div>

              <section aria-labelledby="agent-permissions">
                <h3 id="agent-permissions" className="card-title">
                  权限上限
                </h3>
                <div className="checkbox-list">
                  {permissionList.map((permission) => (
                    <label key={permission.name} title={permission.description}>
                      <input
                        type="checkbox"
                        checked={currentPermissions.includes(permission.name)}
                        onChange={() => setEditPermissions(toggle(currentPermissions, permission.name))}
                      />
                      {permission.name}
                    </label>
                  ))}
                </div>
                {editPermissions !== null ? (
                  <div className="toolbar">
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={busy}
                      onClick={() =>
                        void run("权限已更新；超出上限的令牌权限已自动收窄", () =>
                          api.updateAgent(selected.id, { permissions: editPermissions }),
                        ).then(() => setEditPermissions(null))
                      }
                    >
                      保存权限
                    </Button>
                    <Button size="sm" onClick={() => setEditPermissions(null)}>
                      放弃修改
                    </Button>
                  </div>
                ) : null}
              </section>

              <section aria-labelledby="agent-grants">
                <h3 id="agent-grants" className="card-title">
                  空间授权
                </h3>
                <DataTable
                  caption="空间授权"
                  rows={selected.grants}
                  rowKey={(row) => row.space_id}
                  empty={<p className="muted small">没有任何空间授权。</p>}
                  columns={[
                    { key: "space", header: "空间", render: (row) => <code>{row.space_id}</code> },
                    { key: "access", header: "访问级别", render: (row) => ACCESS_LABELS[row.access] ?? row.access },
                    {
                      key: "actions",
                      header: "操作",
                      render: (row) => (
                        <div className="toolbar">
                          <Button
                            size="sm"
                            disabled={busy}
                            onClick={() =>
                              void run("访问级别已更新", () =>
                                api.setGrant(selected.id, row.space_id, row.access === "read" ? "readwrite" : "read"),
                              )
                            }
                          >
                            {row.access === "read" ? "改为读写" : "改为只读"}
                          </Button>
                          {row.space_id !== selected.default_space_id ? (
                            <Button
                              size="sm"
                              variant="danger"
                              disabled={busy}
                              onClick={() => void run("授权已撤销", () => api.revokeGrant(selected.id, row.space_id))}
                            >
                              撤销
                            </Button>
                          ) : null}
                        </div>
                      ),
                    },
                  ]}
                />
                <form
                  className="toolbar wrap"
                  onSubmit={(event) => {
                    event.preventDefault();
                    if (!grantSpace) return;
                    void run("已授予空间访问", () => api.setGrant(selected.id, grantSpace, grantAccess));
                  }}
                >
                  <label>
                    <span className="sr-only">空间</span>
                    <select className="select" value={grantSpace} onChange={(event) => setGrantSpace(event.target.value)}>
                      <option value="">选择空间…</option>
                      {spaceOptions.map((space) => (
                        <option key={space.id} value={space.id}>
                          {space.name}（{space.id}）
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span className="sr-only">访问级别</span>
                    <select
                      className="select"
                      value={grantAccess}
                      onChange={(event) => setGrantAccess(event.target.value as "read" | "readwrite")}
                    >
                      <option value="read">只读</option>
                      <option value="readwrite">读写</option>
                    </select>
                  </label>
                  <Button type="submit" size="sm" disabled={busy || !grantSpace}>
                    授予访问
                  </Button>
                </form>
              </section>

              <section aria-labelledby="agent-tokens">
                <h3 id="agent-tokens" className="card-title">
                  令牌
                </h3>
                {tokens.error ? (
                  <ErrorState error={tokens.error} onRetry={tokens.reload} />
                ) : (
                  <DataTable
                    caption="令牌列表"
                    rows={tokens.data?.data ?? []}
                    rowKey={(row) => row.id}
                    empty={<p className="muted small">还没有令牌。</p>}
                    columns={tokenColumns}
                  />
                )}
                <form className="stack" onSubmit={(event) => void issueToken(event)}>
                  <div className="grid two">
                    <Field label="令牌名称" id="token-name">
                      <input id="token-name" className="input" maxLength={120} value={tokenName} onChange={(event) => setTokenName(event.target.value)} />
                    </Field>
                    <Field label="有效天数" id="token-days" hint="1–3650 天。轮换时沿用原有效期。">
                      <input
                        id="token-days"
                        className="input narrow"
                        type="number"
                        min={1}
                        max={3650}
                        value={tokenDays}
                        onChange={(event) => setTokenDays(Math.min(3650, Math.max(1, Number(event.target.value) || 1)))}
                      />
                    </Field>
                  </div>
                  <fieldset className="stack">
                    <legend className="field-label">令牌权限（只能是 Agent 权限的子集）</legend>
                    <div className="checkbox-list">
                      {tokenScopeChoices.map((scope) => (
                        <label key={scope}>
                          <input
                            type="checkbox"
                            checked={activeScopes.includes(scope)}
                            onChange={() => setTokenScopes(toggle(activeScopes, scope))}
                          />
                          {scope}
                        </label>
                      ))}
                    </div>
                  </fieldset>
                  <div>
                    <Button type="submit" variant="primary" loading={busy} disabled={tokenScopeChoices.length === 0}>
                      颁发新令牌
                    </Button>
                  </div>
                </form>
              </section>

              <section aria-labelledby="agent-activity">
                <h3 id="agent-activity" className="card-title">
                  最近访问（仅元数据）
                </h3>
                <DataTable
                  caption="Agent 访问记录"
                  rows={logs.data?.data ?? []}
                  rowKey={(row) => String(row.id)}
                  empty={<p className="muted small">该 Agent 还没有访问记录。</p>}
                  columns={[
                    { key: "at", header: "时间", render: (row) => formatDateTime(row.occurred_at, timezone) },
                    { key: "method", header: "方法", render: (row) => row.method },
                    { key: "route", header: "路由", render: (row) => <code>{row.route}</code> },
                    { key: "status", header: "状态", render: (row) => String(row.status_code) },
                    { key: "error", header: "错误码", render: (row) => row.error_code ?? "—" },
                  ]}
                />
              </section>
            </div>
          )}
        </Card>
      </div>

      <Modal
        open={createOpen}
        title="新建 Agent"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <Button onClick={() => setCreateOpen(false)}>取消</Button>
            <Button variant="primary" type="submit" form="create-agent-form" loading={busy}>
              创建
            </Button>
          </>
        }
      >
        <form id="create-agent-form" className="stack" onSubmit={(event) => void createAgent(event)}>
          {formError ? <Notice tone="danger">{formError}</Notice> : null}
          <Field label="名称" id="agent-name">
            <input id="agent-name" className="input" maxLength={120} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
          </Field>
          <Field label="类型" id="agent-type">
            <select id="agent-type" className="select" value={draft.agent_type} onChange={(event) => setDraft({ ...draft, agent_type: event.target.value })}>
              {AGENT_TYPES.map((type) => (
                <option key={type.id} value={type.id}>
                  {type.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="默认空间" id="agent-default-space" hint="Agent 未指定空间时使用它。创建后自动获得读写授权。">
            <select
              id="agent-default-space"
              className="select"
              value={draft.default_space_id}
              onChange={(event) => setDraft({ ...draft, default_space_id: event.target.value })}
            >
              {spaceOptions.map((space) => (
                <option key={space.id} value={space.id}>
                  {space.name}（{space.id}）
                </option>
              ))}
            </select>
          </Field>
          <Field label="描述（可选）" id="agent-description">
            <textarea
              id="agent-description"
              className="textarea"
              rows={2}
              maxLength={500}
              value={draft.description}
              onChange={(event) => setDraft({ ...draft, description: event.target.value })}
            />
          </Field>
          <fieldset className="stack">
            <legend className="field-label">权限上限</legend>
            <div className="checkbox-list">
              {permissionList.map((permission) => (
                <label key={permission.name} title={permission.description}>
                  <input
                    type="checkbox"
                    checked={draft.permissions.includes(permission.name)}
                    onChange={() => setDraft({ ...draft, permissions: toggle(draft.permissions, permission.name) })}
                  />
                  {permission.name}
                </label>
              ))}
            </div>
            <p className="muted small">默认只授予 context、read、search。写入、删除、敏感记忆等权限需要显式勾选。</p>
          </fieldset>
        </form>
      </Modal>

      <Modal
        open={issued !== null}
        title="令牌（只显示这一次）"
        onClose={() => setIssued(null)}
        footer={
          <Button variant="primary" onClick={() => setIssued(null)}>
            我已保存
          </Button>
        }
      >
        {issued ? (
          <div className="stack">
            <Notice tone="warning" title="请立即复制并保存">
              关闭此窗口后，控制台无法再次显示这个令牌。
            </Notice>
            <pre className="code-block" aria-label="新令牌">
              {issued.token}
            </pre>
            <div className="toolbar">
              <Button
                size="sm"
                onClick={() => {
                  void navigator.clipboard?.writeText(issued.token).then(() => toast.notify("已复制到剪贴板", "success"));
                }}
              >
                复制令牌
              </Button>
            </div>
            <p className="muted small">
              令牌名称：{issued.data.name}；权限：{issued.data.scopes.join("、")}；到期：{formatDateTime(issued.data.expires_at, timezone)}
            </p>
          </div>
        ) : null}
      </Modal>

      <Modal
        open={confirmDelete && selected !== null}
        title="删除 Agent"
        onClose={() => setConfirmDelete(false)}
        footer={
          <>
            <Button onClick={() => setConfirmDelete(false)}>取消</Button>
            <Button
              variant="danger"
              loading={busy}
              onClick={() => {
                if (!selected) return;
                const id = selected.id;
                setConfirmDelete(false);
                void run("Agent 已删除，它的所有令牌已撤销", () => api.deleteAgent(id)).then(() => {
                  setSelectedId(null);
                  resetEditors();
                });
              }}
            >
              确认删除
            </Button>
          </>
        }
      >
        <Notice tone="warning">删除会立即撤销该 Agent 的全部令牌，并停止它的所有访问。历史访问记录会保留。</Notice>
      </Modal>
    </>
  );
}
