import { useState, type FormEvent } from "react";
import { useConsole } from "../app/ConsoleContext";
import { api } from "../lib/api/endpoints";
import type { LabContext } from "../lib/api/types";
import { useAsync } from "../lib/hooks";
import { formatDateTime, memoryTypeLabel, truncate } from "../lib/format";
import {
  Button,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  KeyValue,
  NotAvailable,
  Notice,
  PageHeader,
} from "../components/ui";

interface LabRequest {
  query: string;
  limit: number;
  maxChars: number;
}

const EXCLUDED_LABELS: Record<keyof LabContext["excluded"], string> = {
  expired: "已过期",
  sensitive: "敏感内容",
  irrelevant: "相关度不足",
  duplicate: "重复",
  filtered: "被过滤",
};

const RECALL_ROUTES = ["/api/v1/agent/recall", "/api/v1/agent/context", "/api/v1/console/lab/context"];

export function ContextLabPage() {
  const { spaceId, timezone } = useConsole();
  const [draft, setDraft] = useState("");
  const [limit, setLimit] = useState(8);
  const [maxChars, setMaxChars] = useState(600);
  const [request, setRequest] = useState<LabRequest | null>(null);

  const lab = useAsync<LabContext | null>(async () => {
    if (!request) return null;
    const response = await api.labContext({
      query: request.query,
      limit: request.limit,
      max_content_chars: request.maxChars,
    });
    return response.data;
  }, [spaceId, request]);

  const history = useAsync(() => api.opsLogs({ limit: 200 }), [spaceId, request]);

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const query = draft.trim();
    if (query.length === 0) return;
    setRequest({ query, limit, maxChars });
  }

  const recallHistory = (history.data?.data ?? [])
    .filter((row) => RECALL_ROUTES.some((route) => row.route.endsWith(route.replace("/api/v1", ""))) || row.route.includes("/lab/context") || row.route.includes("/recall") || row.route.includes("/context"))
    .filter((row) => row.space_id === null || row.space_id === spaceId)
    .slice(0, 20);

  return (
    <>
      <PageHeader
        title="上下文实验室"
        description="测试记忆召回：查看召回结果、相关度、被排除的原因，以及 Agent 实际会收到的上下文块。"
      />

      <Card title="检索测试">
        <form className="toolbar wrap" onSubmit={submit}>
          <label className="grow">
            <span className="sr-only">测试问题</span>
            <input
              className="input"
              placeholder="例如：我之前准备买什么电脑？"
              value={draft}
              maxLength={500}
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
          <Field label="数量" id="lab-limit">
            <input
              id="lab-limit"
              className="input narrow"
              type="number"
              min={1}
              max={20}
              value={limit}
              onChange={(event) => setLimit(Math.min(20, Math.max(1, Number(event.target.value) || 1)))}
            />
          </Field>
          <Field label="单条最大字数" id="lab-chars">
            <input
              id="lab-chars"
              className="input narrow"
              type="number"
              min={80}
              max={2000}
              value={maxChars}
              onChange={(event) => setMaxChars(Math.min(2000, Math.max(80, Number(event.target.value) || 80)))}
            />
          </Field>
          <Button type="submit" variant="primary">
            运行检索
          </Button>
        </form>
        <p className="muted small">
          显示的是所有者视角的结果，可能包含敏感内容；Agent 通过 /api/v1/agent/context 收到的版本已经过滤敏感内容。
        </p>
      </Card>

      {request === null ? (
        <EmptyState title="输入一个问题开始测试" description="结果、相关度与上下文预览会显示在这里。" />
      ) : lab.error ? (
        <ErrorState error={lab.error} onRetry={lab.reload} />
      ) : lab.loading && !lab.data ? (
        <p className="muted">正在检索…</p>
      ) : lab.data ? (
        <>
          {lab.data.degraded_to_fts ? (
            <Notice tone="warning" title="语义检索不可用，已降级为关键词检索">
              召回质量可能下降。检查嵌入模型与向量索引的状态。
            </Notice>
          ) : null}

          <div className="grid two">
            <Card title="召回结果" subtitle={`检索版本 ${lab.data.retrieval_version}，共 ${lab.data.total} 条`}>
              <DataTable
                caption="召回结果与相关度"
                rows={lab.data.items}
                rowKey={(row) => row.memory_id}
                empty={<EmptyState title="没有召回到记忆" description="换一个问法，或检查这些记忆是否有效、是否敏感。" />}
                columns={[
                  { key: "rank", header: "排名", render: (row) => String((lab.data?.items.indexOf(row) ?? 0) + 1) },
                  { key: "memory", header: "记忆", render: (row) => truncate(row.content, 90) },
                  { key: "type", header: "类型", render: (row) => memoryTypeLabel(row.memory_type) },
                  {
                    key: "score",
                    header: "相关度",
                    render: (row) => (
                      <span className="score">
                        <span className="score-track">
                          <span className="score-fill" style={{ width: `${Math.round(row.relevance_score * 100)}%` }} />
                        </span>
                        {row.relevance_score.toFixed(3)}
                      </span>
                    ),
                  },
                  { key: "source", header: "来源", render: (row) => row.source },
                  { key: "updated", header: "更新时间", render: (row) => formatDateTime(row.updated_at, timezone) },
                ]}
              />
            </Card>

            <Card title="排除统计" subtitle="只显示数量，不显示具体内容">
              <KeyValue
                items={(Object.keys(EXCLUDED_LABELS) as (keyof LabContext["excluded"])[]).map((key) => ({
                  label: EXCLUDED_LABELS[key],
                  value: String(lab.data?.excluded[key] ?? 0),
                }))}
              />
            </Card>
          </div>

          <Card title="上下文预览" subtitle="这是 Agent 会收到的文本块，包裹在 untrusted-data 标签中">
            <Notice tone="info" title="注入测试">
              记忆内容被当作数据对待：文本中的 &amp;、&lt;、&gt; 已被转义，不能关闭上下文标签；模型应把它当作资料，而不是指令。
            </Notice>
            <pre className="code-block context-preview" aria-label="上下文块预览">
              {lab.data.context}
            </pre>
          </Card>
        </>
      ) : null}

      <div className="grid two">
        <Card title="排序与降级说明">
          <ul className="plain-list">
            <li>
              <NotAvailable reason="逐项的排序打分拆解（向量分与关键词分）尚未由服务端返回" />
            </li>
            <li>
              <NotAvailable reason="召回路径（图谱扩展）尚未接入上下文检索" />
            </li>
          </ul>
        </Card>
        <Card title="召回历史" subtitle="只记录路由、状态与延迟，不记录查询内容">
          {history.error ? (
            <ErrorState error={history.error} onRetry={history.reload} />
          ) : (
            <DataTable
              caption="召回历史（仅元数据）"
              rows={recallHistory}
              rowKey={(row) => String(row.id)}
              empty={<EmptyState title="暂无召回记录" />}
              columns={[
                { key: "at", header: "时间", render: (row) => formatDateTime(row.occurred_at, timezone) },
                { key: "route", header: "路由", render: (row) => row.route },
                { key: "status", header: "状态", render: (row) => String(row.status_code) },
                { key: "latency", header: "延迟", render: (row) => `${row.latency_ms} ms` },
              ]}
            />
          )}
        </Card>
      </div>
    </>
  );
}
