import { useState } from "react";
import { useConsole } from "../app/ConsoleContext";
import { api } from "../lib/api/endpoints";
import { useAsync } from "../lib/hooks";
import { formatNumber, statusTone } from "../lib/format";
import {
  Badge,
  Card,
  CodeBlock,
  DataTable,
  EmptyState,
  ErrorState,
  KeyValue,
  LoadingState,
  Notice,
  PageHeader,
} from "../components/ui";

export function ApiPage() {
  const { spaceId } = useConsole();
  const management = useAsync(() => api.apiManagement(), [spaceId]);
  const health = useAsync(() => api.health(), []);
  const [filter, setFilter] = useState("");

  if (management.error) return <ErrorState error={management.error} onRetry={management.reload} />;
  const data = management.data?.data;
  if (!data) return <LoadingState label="正在读取 API 信息…" />;

  const needle = filter.trim().toLowerCase();
  const endpoints = data.endpoints.filter((endpoint) =>
    needle.length === 0 ? true : `${endpoint.method} ${endpoint.path} ${endpoint.summary}`.toLowerCase().includes(needle),
  );

  return (
    <>
      <PageHeader
        title="API 管理"
        description="统一 v1.0 接口，路径前缀 /api/v1。所有接口都需要凭据，并经过统一的错误格式、限流与访问日志。"
        actions={
          <>
            <a className="btn btn-secondary btn-sm" href="/docs" target="_blank" rel="noreferrer">
              交互式文档（Swagger）
            </a>
            <a className="btn btn-ghost btn-sm" href="/openapi.json" target="_blank" rel="noreferrer">
              OpenAPI 规范
            </a>
          </>
        }
      />

      <section className="grid stats" aria-label="API 状态">
        <div className="stat">
          <span className="stat-label">版本</span>
          <strong className="stat-value">{data.version}</strong>
        </div>
        <div className="stat">
          <span className="stat-label">服务状态</span>
          <strong className="stat-value">
            {health.data ? <Badge tone={statusTone(health.data.status)}>{health.data.status}</Badge> : "…"}
          </strong>
        </div>
        <div className="stat">
          <span className="stat-label">接口数量</span>
          <strong className="stat-value">{formatNumber(data.endpoints.length)}</strong>
        </div>
        <div className="stat">
          <span className="stat-label">24 小时请求</span>
          <strong className="stat-value">{formatNumber(data.usage.total)}</strong>
        </div>
      </section>

      <div className="grid two">
        <Card title="限流" subtitle="每分钟，按进程计数">
          <KeyValue
            items={[
              { label: "所有者（每分钟）", value: formatNumber(data.rate_limits.owner_per_minute) },
              { label: "Agent（每分钟）", value: formatNumber(data.rate_limits.agent_per_minute) },
              { label: "认证失败（每分钟）", value: formatNumber(data.rate_limits.auth_failures_per_minute) },
              { label: "统计窗口", value: `${data.rate_limits.window_seconds} 秒（${data.rate_limits.scope}）` },
            ]}
          />
          <p className="muted small">超过限制时返回 429，并附带 Retry-After 响应头。</p>
        </Card>

        <Card title="认证方式">
          <KeyValue items={Object.entries(data.authentication).map(([label, value]) => ({ label, value }))} />
        </Card>
      </div>

      <Card title="统一错误格式" subtitle="所有错误都使用同一个结构">
        <CodeBlock value={data.error_envelope} />
        {data.usage.top_error_codes.length > 0 ? (
          <Notice tone="warning" title="最近 24 小时的错误码">
            {data.usage.top_error_codes.map((item) => `${item.code} × ${item.count}`).join("；")}
          </Notice>
        ) : (
          <Notice tone="success">最近 24 小时没有错误响应。</Notice>
        )}
      </Card>

      <Card
        title="接口列表"
        subtitle={`共 ${data.endpoints.length} 个 /api/v1 接口，当前显示 ${endpoints.length} 个`}
        actions={
          <label>
            <span className="sr-only">筛选接口</span>
            <input
              className="input"
              type="search"
              placeholder="按方法、路径或说明筛选"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            />
          </label>
        }
      >
        <DataTable
          caption="API 接口列表"
          rows={endpoints}
          rowKey={(row) => `${row.method} ${row.path}`}
          empty={<EmptyState title="没有匹配的接口" />}
          columns={[
            { key: "method", header: "方法", render: (row) => <Badge tone="info">{row.method}</Badge> },
            { key: "path", header: "路径", render: (row) => <code>{row.path}</code> },
            { key: "summary", header: "说明", render: (row) => row.summary },
            { key: "tags", header: "分组", render: (row) => row.tags.join("、") || "—" },
          ]}
        />
      </Card>

      <Card title="接入提示">
        <p>
          应用接入应当使用 Agent 令牌（在「Agent 管理」中颁发），并通过 <code>X-JarvisMind-Space</code> 请求头选择已授权的记忆空间。
          所有者令牌用于控制台与运维，不应交给第三方应用。
        </p>
      </Card>
    </>
  );
}
