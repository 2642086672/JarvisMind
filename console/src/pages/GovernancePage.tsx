import { useState } from "react";
import { useConsole } from "../app/ConsoleContext";
import { describeError } from "../lib/api/client";
import { api } from "../lib/api/endpoints";
import type { Candidate, ForgetRequest, ModificationRequest } from "../lib/api/types";
import { useAsync } from "../lib/hooks";
import { formatDateTime, formatNumber, forgetStatusLabel, memoryTypeLabel, statusTone } from "../lib/format";
import {
  Badge,
  Button,
  Card,
  Column,
  DataTable,
  ErrorState,
  KeyValue,
  LoadingState,
  NotAvailable,
  Notice,
  PageHeader,
  StatCard,
  useToast,
} from "../components/ui";

const SENSITIVE_STATUS = "PENDING_AUTHORIZATION";

function isAgentOrigin(label: string | undefined): boolean {
  return (label ?? "").toLowerCase().startsWith("agent:");
}

function onOff(value: boolean): string {
  return value ? "开启" : "关闭";
}

export function GovernancePage() {
  const { spaceId, timezone } = useConsole();
  const toast = useToast();
  const overview = useAsync(() => api.governanceOverview(), [spaceId]);
  const pending = useAsync(() => api.listCandidates({ status: "PENDING", page_size: 50 }), [spaceId]);
  const sensitive = useAsync(() => api.listCandidates({ status: SENSITIVE_STATUS, page_size: 50 }), [spaceId]);
  const forgets = useAsync(() => api.listForgetRequests({ status: "PENDING", page_size: 50 }), [spaceId]);
  const modifications = useAsync(() => api.listModificationRequests({ status: "PENDING", page_size: 50 }), [spaceId]);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  function reloadAll(): void {
    overview.reload();
    pending.reload();
    sensitive.reload();
    forgets.reload();
    modifications.reload();
  }

  async function decideCandidate(candidate: Candidate, decision: "approve" | "reject"): Promise<void> {
    setBusy(candidate.id);
    setActionError(null);
    try {
      if (decision === "approve") {
        await api.approveCandidate(candidate.id, {});
        toast.notify("候选已批准", "success");
      } else {
        await api.rejectCandidate(candidate.id, { reason: "rejected in the governance centre" });
        toast.notify("候选已拒绝", "neutral");
      }
      reloadAll();
    } catch (cause) {
      setActionError(describeError(cause));
    } finally {
      setBusy(null);
    }
  }

  async function decideForget(request: ForgetRequest, decision: "approve" | "reject"): Promise<void> {
    if (!isAgentOrigin(request.requester_label)) {
      setActionError("这是所有者在其它会话创建的遗忘请求。它的确认令牌只返回一次，请在创建它的会话中处理。");
      return;
    }
    setBusy(request.id);
    setActionError(null);
    try {
      if (decision === "approve") {
        await api.approveForgetRequest(request.id, {});
        toast.notify("遗忘已执行，记忆已标记为已遗忘", "success");
      } else {
        await api.rejectForgetRequest(request.id, {});
        toast.notify("已拒绝遗忘请求，记忆保持不变", "neutral");
      }
      reloadAll();
    } catch (cause) {
      setActionError(describeError(cause));
    } finally {
      setBusy(null);
    }
  }

  if (overview.error) return <ErrorState error={overview.error} onRetry={overview.reload} />;
  const data = overview.data?.data;
  if (!data) return <LoadingState label="正在读取治理概览…" />;

  const candidateColumns: Column<Candidate>[] = [
    { key: "content", header: "内容", render: (row) => row.content },
    { key: "type", header: "类型", render: (row) => memoryTypeLabel(row.memory_type) },
    {
      key: "categories",
      header: "敏感类别",
      render: (row) => (row.sensitive_categories.length > 0 ? row.sensitive_categories.join("、") : "—"),
    },
    { key: "extracted", header: "提取时间", render: (row) => formatDateTime(row.extracted_at, timezone) },
    {
      key: "actions",
      header: "操作",
      render: (row) => (
        <div className="toolbar">
          <Button size="sm" variant="primary" loading={busy === row.id} onClick={() => void decideCandidate(row, "approve")}>
            批准
          </Button>
          <Button size="sm" variant="danger" disabled={busy === row.id} onClick={() => void decideCandidate(row, "reject")}>
            拒绝
          </Button>
        </div>
      ),
    },
  ];

  const forgetColumns: Column<ForgetRequest>[] = [
    {
      key: "memory",
      header: "记忆 ID",
      render: (row) => <code>{row.memory_id}</code>,
    },
    {
      key: "source",
      header: "发起方",
      render: (row) => (isAgentOrigin(row.requester_label) ? "Agent" : "所有者（其它会话）"),
    },
    {
      key: "status",
      header: "状态",
      render: (row) => <Badge tone={statusTone(row.status)}>{forgetStatusLabel(row.status)}</Badge>,
    },
    { key: "requested", header: "申请时间", render: (row) => formatDateTime(row.requested_at, timezone) },
    {
      key: "actions",
      header: "操作",
      render: (row) =>
        isAgentOrigin(row.requester_label) ? (
          <div className="toolbar">
            <Button size="sm" variant="danger" disabled={busy === row.id} onClick={() => void decideForget(row, "approve")}>
              批准遗忘
            </Button>
            <Button size="sm" disabled={busy === row.id} onClick={() => void decideForget(row, "reject")}>
              拒绝
            </Button>
          </div>
        ) : (
          <span className="muted small">在创建它的会话中处理</span>
        ),
    },
  ];

  const modificationColumns: Column<ModificationRequest>[] = [
    { key: "memory", header: "记忆 ID", render: (row) => <code>{row.memory_id}</code> },
    { key: "kind", header: "类型", render: (row) => row.request_kind },
    { key: "status", header: "状态", render: (row) => <Badge tone={statusTone(row.status)}>{row.status}</Badge> },
    {
      key: "note",
      header: "说明",
      render: () => <span className="muted small">确认令牌只返回一次，在创建它的会话中批准</span>,
    },
  ];

  return (
    <>
      <PageHeader
        title="治理中心"
        description="敏感记忆、待确认候选与遗忘请求都在这里审批。所有决定由服务端执行并写入审计日志；控制台只负责展示与提交。"
      />
      {actionError ? <Notice tone="danger">{actionError}</Notice> : null}

      <section className="grid stats" aria-label="待治理数量">
        <StatCard label="待确认候选" value={formatNumber(data.pending.candidates)} tone={data.pending.candidates > 0 ? "warning" : "neutral"} />
        <StatCard
          label="待授权敏感记忆"
          value={formatNumber(data.pending.sensitive_authorization)}
          tone={data.pending.sensitive_authorization > 0 ? "warning" : "neutral"}
        />
        <StatCard label="待审批遗忘" value={formatNumber(data.pending.forget_requests)} />
        <StatCard label="待审批修改" value={formatNumber(data.pending.modification_requests)} />
        <StatCard label="已拒绝候选" value={formatNumber(data.pending.rejected_candidates)} tone="danger" />
      </section>

      <div className="grid two">
        <Card title="策略" subtitle="由服务端配置决定，控制台只读展示">
          <KeyValue
            items={[
              { label: "凭据拦截", value: data.policies.credential_gate },
              { label: "敏感事实", value: data.policies.sensitive_facts },
              { label: "自动记忆配置标记（不控制显式记忆写入）", value: onOff(data.policies.auto_save_enabled) },
              { label: "直接写入（接口）", value: onOff(data.policies.direct_write_enabled) },
              { label: "P1 治理", value: onOff(data.policies.p1_governance_enabled) },
              { label: "生命周期：导入", value: onOff(data.policies.lifecycle.import) },
              { label: "生命周期：合并", value: onOff(data.policies.lifecycle.merge) },
              { label: "生命周期：摘要审核", value: onOff(data.policies.lifecycle.summary_review) },
              { label: "生命周期：更正处理", value: onOff(data.policies.lifecycle.correction_resolve) },
              { label: "非人类主体", value: data.policies.non_human_actors },
            ]}
          />
        </Card>

        <Card title="记忆分类" subtitle="与 JME 的记忆类型一一对应，不另建分类">
          <div className="toolbar wrap">
            {data.memory_types.map((type) => (
              <Badge key={type} tone="info">
                {memoryTypeLabel(type)}
              </Badge>
            ))}
          </div>
          <p className="muted small">生命周期状态为有效、归档、已遗忘、已合并。归档与恢复在记忆详情中操作。</p>
          <ul className="plain-list">
            <li>
              <NotAvailable reason="治理规则的增删改、版本与启停尚未在服务端实现" />
            </li>
            <li>
              <NotAvailable reason="规则测试与规则执行记录尚未实现" />
            </li>
          </ul>
        </Card>
      </div>

      <Card title="审批中心" subtitle="敏感记忆、候选、遗忘与修改请求">
        <h3 className="card-title">待授权的敏感记忆</h3>
        <DataTable
          caption="待授权的敏感记忆"
          columns={candidateColumns}
          rows={sensitive.data?.data ?? []}
          rowKey={(row) => row.id}
          empty={<p className="muted small">没有等待授权的敏感记忆。</p>}
        />

        <h3 className="card-title">待确认的候选</h3>
        <DataTable
          caption="待确认的候选"
          columns={candidateColumns}
          rows={pending.data?.data ?? []}
          rowKey={(row) => row.id}
          empty={<p className="muted small">没有待确认的候选。</p>}
        />

        <h3 className="card-title">待审批的遗忘请求</h3>
        <DataTable
          caption="待审批的遗忘请求"
          columns={forgetColumns}
          rows={forgets.data?.data ?? []}
          rowKey={(row) => row.id}
          empty={<p className="muted small">没有待审批的遗忘请求。</p>}
        />

        <h3 className="card-title">待审批的修改请求</h3>
        <DataTable
          caption="待审批的修改请求"
          columns={modificationColumns}
          rows={modifications.data?.data ?? []}
          rowKey={(row) => row.id}
          empty={<p className="muted small">没有待审批的修改请求。</p>}
        />
      </Card>

      <Card title="最近治理与审计事件" subtitle="来自记忆审计日志，只显示操作类型与时间">
        <DataTable
          caption="最近审计事件"
          rows={data.recent_audit}
          rowKey={(row) => `${row.at}-${row.action}-${row.memory_id ?? ""}`}
          empty={<p className="muted small">暂无审计事件。</p>}
          columns={[
            { key: "action", header: "操作", render: (row) => row.action },
            { key: "actor", header: "执行者", render: (row) => row.actor ?? "—" },
            { key: "memory", header: "记忆 ID", render: (row) => (row.memory_id ? <code>{row.memory_id}</code> : "—") },
            { key: "at", header: "时间", render: (row) => formatDateTime(row.at, timezone) },
          ]}
        />
      </Card>
    </>
  );
}
