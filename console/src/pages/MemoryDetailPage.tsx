import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useConsole } from "../app/ConsoleContext";
import { ApiError, describeError } from "../lib/api/client";
import { api, type MemoryPatch } from "../lib/api/endpoints";
import type { ForgetCreated, ForgetRequest, ModificationRequest } from "../lib/api/types";
import { useAsync } from "../lib/hooks";
import { hasPendingToken, rememberPendingToken, takePendingToken } from "../lib/pendingTokens";
import {
  forgetStatusLabel,
  formatDateTime,
  memoryStatusLabel,
  memoryTypeLabel,
  statusTone,
} from "../lib/format";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  KeyValue,
  LoadingState,
  Modal,
  NotAvailable,
  Notice,
  PageHeader,
  useToast,
} from "../components/ui";

const EDITABLE_STATUS = ["ACTIVE", "ARCHIVED"] as const;

function isAgentRequest(label: string | undefined): boolean {
  return (label ?? "").startsWith("agent:");
}

function isAgentPendingConflict(cause: unknown): boolean {
  if (!(cause instanceof ApiError) || typeof cause.details !== "object" || cause.details === null) return false;
  return (cause.details as { code?: unknown }).code === "agent_request_pending";
}

export function MemoryDetailPage() {
  const { memoryId = "" } = useParams();
  const { spaceId, timezone } = useConsole();
  const toast = useToast();

  const memory = useAsync(() => api.getMemory(memoryId), [spaceId, memoryId]);
  const audit = useAsync(() => api.memoryAudit(memoryId), [spaceId, memoryId]);
  const forgets = useAsync(
    () => api.listForgetRequests({ memory_id: memoryId, page: 1, page_size: 20 }),
    [spaceId, memoryId],
  );
  const modifications = useAsync(
    () => api.listModificationRequests({ memory_id: memoryId, page: 1, page_size: 20 }),
    [spaceId, memoryId],
  );

  const [editSummary, setEditSummary] = useState<string | null>(null);
  const [editImportance, setEditImportance] = useState<string | null>(null);
  const [editStatus, setEditStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [forgetOpen, setForgetOpen] = useState(false);
  const [forgetReason, setForgetReason] = useState("");
  const [forgetDraft, setForgetDraft] = useState<ForgetCreated | null>(null);

  function refreshAll(): void {
    memory.reload();
    audit.reload();
    forgets.reload();
    modifications.reload();
  }

  async function saveDirect(): Promise<void> {
    const current = memory.data?.data;
    if (!current) return;
    const body: MemoryPatch = {};
    if (editSummary !== null && editSummary !== (current.summary ?? "")) body.summary = editSummary;
    if (editImportance !== null && Number(editImportance) !== current.importance) {
      body.importance = Number(editImportance);
    }
    if (editStatus !== null && editStatus !== current.status) {
      body.status = editStatus as MemoryPatch["status"];
    }
    if (Object.keys(body).length === 0) {
      toast.notify("没有需要保存的修改。");
      return;
    }
    setBusy(true);
    setActionError(null);
    try {
      await api.patchMemory(memoryId, body);
      setEditSummary(null);
      setEditImportance(null);
      setEditStatus(null);
      toast.notify("已保存修改。", "success");
      refreshAll();
    } catch (cause) {
      setActionError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  async function submitGovernedCorrection(): Promise<void> {
    const current = memory.data?.data;
    if (!current) return;
    const proposed: Record<string, unknown> = {};
    if (editSummary !== null) proposed.summary = editSummary;
    if (editImportance !== null) proposed.importance = Number(editImportance);
    if (Object.keys(proposed).length === 0) {
      toast.notify("请先修改摘要或重要性。");
      return;
    }
    setBusy(true);
    setActionError(null);
    try {
      const created = await api.createModificationRequest(memoryId, {
        proposed_changes: proposed,
        request_kind: "UPDATE",
        reason: "控制台修改",
      });
      rememberPendingToken(created.data.request.id, created.data.confirmation_token);
      toast.notify("修改请求已创建，请在“待审批的修改请求”中批准。", "success");
      setEditSummary(null);
      setEditImportance(null);
      refreshAll();
    } catch (cause) {
      setActionError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  async function startForget(): Promise<void> {
    setBusy(true);
    setActionError(null);
    try {
      const response = await api.createForgetRequest(memoryId, { reason: forgetReason.trim() || undefined });
      rememberPendingToken(response.data.id, response.data.confirmation_token);
      setForgetDraft(response.data);
      refreshAll();
    } catch (cause) {
      setActionError(
        isAgentPendingConflict(cause)
          ? "这条记忆已有 Agent 发起的待审批遗忘请求。请在下方“待确认的遗忘请求”中直接批准或拒绝它。"
          : describeError(cause),
      );
    } finally {
      setBusy(false);
    }
  }

  async function confirmForget(request: ForgetRequest): Promise<void> {
    const token = takePendingToken(request.id);
    setBusy(true);
    setActionError(null);
    try {
      await api.approveForgetRequest(request.id, token ? { confirmation_token: token } : {});
      setForgetOpen(false);
      setForgetDraft(null);
      setForgetReason("");
      toast.notify("记忆已遗忘。之后任何接口都不会再返回它。", "success");
      refreshAll();
    } catch (cause) {
      setActionError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  async function rejectForget(request: ForgetRequest): Promise<void> {
    setBusy(true);
    setActionError(null);
    try {
      const token = takePendingToken(request.id);
      await api.rejectForgetRequest(request.id, token ? { confirmation_token: token } : {});
      toast.notify("已拒绝遗忘请求，记忆保持不变。");
      refreshAll();
    } catch (cause) {
      setActionError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  async function approveModification(request: ModificationRequest): Promise<void> {
    const token = takePendingToken(request.id);
    if (!token) {
      setActionError("这个修改请求的确认令牌只在创建时返回一次，本页面已没有它。请在创建该请求的会话中批准。");
      return;
    }
    setBusy(true);
    setActionError(null);
    try {
      await api.approveModificationRequest(request.id, { confirmation_token: token });
      toast.notify("修改请求已批准。", "success");
      refreshAll();
    } catch (cause) {
      setActionError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  if (memory.error) return <ErrorState error={memory.error} onRetry={memory.reload} />;
  const current = memory.data?.data;
  if (!current) return <LoadingState label="正在读取记忆…" />;

  const pendingForgets = (forgets.data?.data ?? []).filter((item) => item.status === "PENDING");
  const pendingModifications = (modifications.data?.data ?? []).filter((item) => item.status === "PENDING");
  const isForgotten = current.status === "DELETED";

  return (
    <>
      <PageHeader
        title="记忆详情"
        description={`完整 ID：${current.id}`}
        actions={
          <>
            <Link className="btn btn-ghost btn-sm" to="/memories">
              返回列表
            </Link>
            {!isForgotten ? (
              <Button variant="danger" size="sm" onClick={() => setForgetOpen(true)}>
                遗忘这条记忆
              </Button>
            ) : null}
          </>
        }
      />

      {actionError ? <Notice tone="danger">{actionError}</Notice> : null}

      <div className="grid two">
        <Card title="内容">
          <p className="memory-content">{current.content}</p>
          {current.summary ? <p className="muted">摘要：{current.summary}</p> : null}
          <KeyValue
            items={[
              { label: "类型", value: memoryTypeLabel(current.memory_type) },
              { label: "状态", value: <Badge tone={statusTone(current.status)}>{memoryStatusLabel(current.status)}</Badge> },
              { label: "重要性", value: current.importance.toFixed(1) },
              { label: "置信度", value: current.confidence.toFixed(2) },
              { label: "创建时间", value: formatDateTime(current.created_at, timezone) },
              { label: "修改时间", value: formatDateTime(current.updated_at, timezone) },
              { label: "最近访问", value: formatDateTime(current.accessed_at, timezone) },
              { label: "过期时间", value: formatDateTime(current.expires_at, timezone) },
              { label: "来源", value: current.source_id ?? "—" },
              { label: "遗忘时间", value: formatDateTime(current.forgotten_at, timezone) },
              { label: "合并到", value: current.merged_into ?? "—" },
            ]}
          />
        </Card>

        <Card title="编辑" subtitle="所有修改都经过服务端：直接写入需要服务端开启对应开关，受保护记忆使用修改请求并由所有者批准">
          {isForgotten ? (
            <Notice tone="warning">这条记忆已遗忘，不能再编辑。</Notice>
          ) : (
            <form
              className="stack"
              onSubmit={(event) => {
                event.preventDefault();
                void saveDirect();
              }}
            >
              <Field label="摘要" id="edit-summary">
                <textarea
                  id="edit-summary"
                  className="textarea"
                  rows={3}
                  maxLength={5000}
                  value={editSummary ?? current.summary ?? ""}
                  onChange={(event) => setEditSummary(event.target.value)}
                />
              </Field>
              <Field label="重要性（0–10）" id="edit-importance">
                <input
                  id="edit-importance"
                  className="input"
                  type="number"
                  min={0}
                  max={10}
                  step={0.1}
                  value={editImportance ?? String(current.importance)}
                  onChange={(event) => setEditImportance(event.target.value)}
                />
              </Field>
              <Field label="状态" id="edit-status">
                <select
                  id="edit-status"
                  className="select"
                  value={editStatus ?? current.status}
                  onChange={(event) => setEditStatus(event.target.value)}
                >
                  {current.status === "ACTIVE" || current.status === "ARCHIVED" ? (
                    EDITABLE_STATUS.map((status) => (
                      <option key={status} value={status}>
                        {memoryStatusLabel(status)}
                      </option>
                    ))
                  ) : (
                    <option value={current.status}>{memoryStatusLabel(current.status)}（只读）</option>
                  )}
                </select>
              </Field>
              <div className="toolbar wrap">
                <Button type="submit" variant="primary" loading={busy}>
                  直接保存
                </Button>
                <Button type="button" onClick={() => void submitGovernedCorrection()} disabled={busy}>
                  提交为修改请求
                </Button>
              </div>
              <p className="muted small">
                分类（记忆类型）暂不支持修改：<NotAvailable reason="服务端的记忆更新接口不包含类型字段" />
              </p>
            </form>
          )}
        </Card>
      </div>

      <div className="grid two">
        <Card title="待确认的遗忘请求" subtitle="Agent 发起的请求只能由所有者批准或拒绝">
          {pendingForgets.length === 0 ? (
            <EmptyState title="没有待确认的遗忘请求" />
          ) : (
            <ul className="plain-list">
              {pendingForgets.map((request) => (
                <li key={request.id} className="row-item">
                  <div>
                    <Badge tone="warning">{forgetStatusLabel(request.status)}</Badge>{" "}
                    <span className="muted small">
                      来源：{isAgentRequest(request.requester_label) ? "Agent" : "控制台/所有者"} ·{" "}
                      {formatDateTime(request.requested_at, timezone)}
                    </span>
                    {request.reason ? <p className="small">原因：{request.reason}</p> : null}
                  </div>
                  <div className="toolbar">
                    <Button size="sm" variant="danger" onClick={() => void confirmForget(request)} disabled={busy}>
                      批准遗忘
                    </Button>
                    <Button size="sm" onClick={() => void rejectForget(request)} disabled={busy}>
                      拒绝
                    </Button>
                  </div>
                  {!isAgentRequest(request.requester_label) && !hasPendingToken(request.id) ? (
                    <p className="small muted">
                      这是在其它会话创建的请求，确认令牌只返回过一次，本页面无法批准；请在创建该请求的会话中确认。
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
          {forgets.data && forgets.data.data.length > 0 ? (
            <details className="history">
              <summary>全部遗忘请求（{forgets.data.data.length}）</summary>
              <DataTable
                caption="遗忘请求历史"
                rows={forgets.data.data}
                rowKey={(row) => row.id}
                columns={[
                  {
                    key: "status",
                    header: "状态",
                    render: (row) => <Badge tone={statusTone(row.status)}>{forgetStatusLabel(row.status)}</Badge>,
                  },
                  { key: "source", header: "来源", render: (row) => (isAgentRequest(row.requester_label) ? "Agent" : "所有者") },
                  { key: "requested", header: "申请时间", render: (row) => formatDateTime(row.requested_at, timezone) },
                  { key: "decided", header: "决定时间", render: (row) => formatDateTime(row.decided_at, timezone) },
                ]}
              />
            </details>
          ) : null}
        </Card>

        <Card title="待审批的修改请求">
          {pendingModifications.length === 0 ? (
            <EmptyState title="没有待审批的修改请求" description="受保护记忆的修改需要先提交修改请求，再由所有者批准。" />
          ) : (
            <ul className="plain-list">
              {pendingModifications.map((request) => (
                <li key={request.id} className="row-item">
                  <div>
                    <Badge tone="warning">{request.status}</Badge>{" "}
                    <span className="muted small">{request.request_kind}</span>
                  </div>
                  <div className="toolbar">
                    <Button size="sm" variant="primary" onClick={() => void approveModification(request)} disabled={busy}>
                      批准
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => {
                        setBusy(true);
                        api
                          .rejectModificationRequest(request.id, {})
                          .then(() => {
                            toast.notify("已拒绝修改请求。");
                            refreshAll();
                          })
                          .catch((cause: unknown) => setActionError(describeError(cause)))
                          .finally(() => setBusy(false));
                      }}
                      disabled={busy}
                    >
                      拒绝
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid two">
        <Card title="修改历史" subtitle="来自记忆审计日志（服务端记录的操作）">
          {audit.loading && !audit.data ? (
            <LoadingState label="正在读取审计日志…" />
          ) : audit.error ? (
            <ErrorState error={audit.error} onRetry={audit.reload} />
          ) : (
            <DataTable
              caption="审计日志"
              rows={audit.data?.data ?? []}
              rowKey={(row) => `${row.created_at}-${row.action}-${row.actor ?? ""}`}
              columns={[
                { key: "action", header: "操作", render: (row) => row.action },
                { key: "actor", header: "执行者", render: (row) => row.actor ?? "—" },
                { key: "at", header: "时间", render: (row) => formatDateTime(row.created_at, timezone) },
              ]}
            />
          )}
        </Card>

        <Card title="关联信息">
          <ul className="plain-list">
            <li>
              <NotAvailable reason="按记忆反查图谱节点需要新接口，目前不提供" />
            </li>
            <li>
              <NotAvailable reason="记忆关联（相似记忆）需要 MCE 的按记忆查询接口" />
            </li>
            <li>
              <NotAvailable reason="访问历史按记忆记录的数据尚未存在；API 访问日志只记录路由，不记录记忆" />
            </li>
            <li>
              <NotAvailable reason="治理记录按记忆汇总的接口尚未提供；可在治理中心查看全部待审批事项" />
            </li>
          </ul>
        </Card>
      </div>

      <Modal
        open={forgetOpen}
        title="遗忘这条记忆"
        onClose={() => {
          setForgetOpen(false);
          setForgetDraft(null);
        }}
        footer={
          forgetDraft ? (
            <>
              <Button onClick={() => setForgetDraft(null)} disabled={busy}>
                取消
              </Button>
              <Button variant="danger" loading={busy} onClick={() => void confirmForget(forgetDraft)}>
                确认遗忘
              </Button>
            </>
          ) : (
            <>
              <Button onClick={() => setForgetOpen(false)} disabled={busy}>
                取消
              </Button>
              <Button variant="danger" loading={busy} onClick={() => void startForget()}>
                提交遗忘请求
              </Button>
            </>
          )
        }
      >
        {forgetDraft ? (
          <Notice tone="warning" title="请再次确认">
            遗忘会把这条记忆标记为已遗忘，并从召回、图谱与导出中移除。这一步不可撤销。点击“确认遗忘”后才会真正执行。
          </Notice>
        ) : (
          <div className="stack">
            <Field label="原因（可选）" id="forget-reason">
              <input
                id="forget-reason"
                className="input"
                maxLength={1000}
                value={forgetReason}
                onChange={(event) => setForgetReason(event.target.value)}
              />
            </Field>
            <p className="muted small">第一步只会登记遗忘请求，记忆此时不会被修改。</p>
          </div>
        )}
      </Modal>
    </>
  );
}
