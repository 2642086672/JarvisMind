import { useState } from "react";
import { useConsole } from "../app/ConsoleContext";
import { api } from "../lib/api/endpoints";
import type { AccessLogItem } from "../lib/api/types";
import { useAsync } from "../lib/hooks";
import { formatDateTime, formatNumber, statusTone, truncate } from "../lib/format";
import {
  Badge,
  Card,
  CodeBlock,
  DataTable,
  EmptyState,
  ErrorState,
  KeyValue,
  LoadingState,
  NotAvailable,
  PageHeader,
  Pagination,
  StatCard,
} from "../components/ui";

const PAGE_SIZE = 50;

type Family = "" | "2xx" | "4xx" | "5xx";

interface LogPage {
  data: AccessLogItem[];
  meta: { total: number; limit: number; offset: number };
}

export function OperationsPage() {
  const { spaceId, timezone } = useConsole();
  const components = useAsync(() => api.opsComponents(), [spaceId]);
  const health = useAsync(() => api.health(), []);
  const info = useAsync(() => api.systemInfo(), []);
  const worker = useAsync(() => api.graphWorkerStatus(), [spaceId]);
  const sync = useAsync(() => api.graphSyncStatus(), [spaceId]);
  const jmr = useAsync(() => api.jmrStatus(), [spaceId]);
  const tasks = useAsync(() => api.opsTasks(), [spaceId]);
  const summary = useAsync(() => api.accessSummary(24), [spaceId]);
  const [family, setFamily] = useState<Family>("");
  const [page, setPage] = useState(1);

  const logs = useAsync<LogPage>(
    () =>
      api.opsLogs({
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
        status_family: family || undefined,
      }),
    [family, page],
  );

  const stats = summary.data?.data;
  const logTotal = logs.data?.meta.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(logTotal / PAGE_SIZE));

  return (
    <>
      <PageHeader
        title="系统运维"
        description="组件状态、后台任务、接口访问与健康检查。日志只包含元数据（方法、路由、状态、延迟），不包含请求内容或令牌。"
        actions={
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => {
            components.reload();
            health.reload();
            worker.reload();
            sync.reload();
            jmr.reload();
            tasks.reload();
            summary.reload();
            logs.reload();
          }}>
            刷新
          </button>
        }
      />

      <Card title="组件状态" subtitle="JME、JMR、JKG、MCE、API、数据库与后台任务">
        {components.error ? (
          <ErrorState error={components.error} onRetry={components.reload} />
        ) : !components.data ? (
          <LoadingState label="正在检查组件…" />
        ) : (
          <DataTable
            caption="组件状态"
            rows={components.data.data}
            rowKey={(row) => row.component}
            empty={<EmptyState title="没有组件信息" />}
            columns={[
              { key: "component", header: "组件", render: (row) => row.component },
              { key: "status", header: "状态", render: (row) => <Badge tone={statusTone(row.status)}>{row.status}</Badge> },
              {
                key: "detail",
                header: "详情",
                render: (row) => (row.seconds !== undefined ? `${formatNumber(row.seconds)} 秒` : row.version ?? "—"),
              },
            ]}
          />
        )}
      </Card>

      <div className="grid two">
        <Card title="API 健康" subtitle="健康检查不需要令牌">
          {health.error ? (
            <ErrorState error={health.error} onRetry={health.reload} />
          ) : health.data ? (
            <KeyValue
              items={[
                { label: "状态", value: <Badge tone={statusTone(health.data.status)}>{health.data.status}</Badge> },
                { label: "数据库连接", value: health.data.database_connected ? "正常" : "异常" },
                { label: "架构版本", value: String(health.data.schema_version) },
                { label: "版本", value: health.data.version },
              ]}
            />
          ) : (
            <LoadingState label="正在检查 API…" />
          )}
        </Card>

        <Card title="运行环境" subtitle="来自系统信息接口">
          {info.error ? (
            <ErrorState error={info.error} onRetry={info.reload} />
          ) : info.data ? (
            <KeyValue
              items={[
                { label: "Python", value: info.data.python_version },
                { label: "数据库类型", value: info.data.database_type },
                { label: "运行状态", value: info.data.status },
                { label: "运行时长", value: `${formatNumber(Math.floor(info.data.uptime_seconds))} 秒` },
              ]}
            />
          ) : (
            <LoadingState label="正在读取系统信息…" />
          )}
        </Card>
      </div>

      <section className="grid stats" aria-label="最近 24 小时接口访问">
        <StatCard label="请求数（24 小时）" value={formatNumber(stats?.total ?? 0)} />
        <StatCard label="成功（2xx）" value={formatNumber(stats?.by_status_family["2xx"] ?? 0)} tone="success" />
        <StatCard label="客户端错误（4xx）" value={formatNumber(stats?.by_status_family["4xx"] ?? 0)} tone="warning" />
        <StatCard label="服务端错误（5xx）" value={formatNumber(stats?.by_status_family["5xx"] ?? 0)} tone="danger" />
        <StatCard label="平均延迟" value={stats ? `${stats.average_latency_ms} ms` : "—"} />
      </section>

      <div className="grid two">
        <Card title="后台任务" subtitle="由服务端报告，只读">
          {tasks.error ? (
            <ErrorState error={tasks.error} onRetry={tasks.reload} />
          ) : tasks.data ? (
            <>
              <CodeBlock value={tasks.data.data.workers} />
              <ul className="plain-list">
                <li>
                  <NotAvailable reason="任务队列表尚未实现（JME 没有 task_queue 数据表）" />
                </li>
                <li>
                  <NotAvailable reason="重试队列与死信队列尚未实现" />
                </li>
              </ul>
            </>
          ) : (
            <LoadingState label="正在读取任务…" />
          )}
        </Card>

        <Card title="图谱同步与 JMR" subtitle="只读状态；同步由服务端单次执行，不会自动运行">
          {worker.error || sync.error || jmr.error ? (
            <ErrorState error={worker.error ?? sync.error ?? jmr.error} onRetry={() => {
              worker.reload();
              sync.reload();
              jmr.reload();
            }} />
          ) : (
            <>
              <h3 className="card-title">图谱 Worker</h3>
              <CodeBlock value={worker.data ?? {}} />
              <h3 className="card-title">图谱同步</h3>
              <CodeBlock value={sync.data ?? {}} />
              <h3 className="card-title">JMR 治理</h3>
              <CodeBlock value={jmr.data ?? {}} />
            </>
          )}
        </Card>
      </div>

      <Card
        title="接口访问记录"
        subtitle="仅元数据：没有请求体、查询串、原始 IP 或令牌"
        actions={
          <label className="toolbar">
            <span className="small">状态</span>
            <select
              className="select"
              value={family}
              onChange={(event) => {
                setFamily(event.target.value as Family);
                setPage(1);
              }}
            >
              <option value="">全部</option>
              <option value="2xx">2xx 成功</option>
              <option value="4xx">4xx 客户端错误</option>
              <option value="5xx">5xx 服务端错误</option>
            </select>
          </label>
        }
      >
        {logs.error ? (
          <ErrorState error={logs.error} onRetry={logs.reload} />
        ) : (
          <>
            <DataTable
              caption="接口访问记录"
              rows={logs.data?.data ?? []}
              rowKey={(row) => String(row.id)}
              empty={<EmptyState title="没有访问记录" />}
              columns={[
                { key: "at", header: "时间", render: (row) => formatDateTime(row.occurred_at, timezone) },
                { key: "principal", header: "主体", render: (row) => (row.agent_id ? `Agent ${truncate(row.agent_id, 14)}` : row.principal_kind) },
                { key: "method", header: "方法", render: (row) => row.method },
                { key: "route", header: "路由", render: (row) => <code>{row.route}</code> },
                { key: "status", header: "状态", render: (row) => <Badge tone={row.status_code >= 500 ? "danger" : row.status_code >= 400 ? "warning" : "success"}>{row.status_code}</Badge> },
                { key: "latency", header: "延迟", render: (row) => `${row.latency_ms} ms` },
                { key: "error", header: "错误码", render: (row) => row.error_code ?? "—" },
              ]}
            />
            <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
            <p className="muted small">共 {formatNumber(logTotal)} 条记录。</p>
          </>
        )}
      </Card>

      <div className="grid two">
        <Card title="错误与热点路由（24 小时）">
          {stats ? (
            <div className="grid two">
              <div>
                <h3 className="card-title">错误码</h3>
                {stats.top_error_codes.length === 0 ? (
                  <p className="muted small">没有错误。</p>
                ) : (
                  <ul className="plain-list">
                    {stats.top_error_codes.map((item) => (
                      <li key={item.code}>
                        <code>{item.code}</code> × {formatNumber(item.count)}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div>
                <h3 className="card-title">热点路由</h3>
                {stats.top_routes.length === 0 ? (
                  <p className="muted small">没有请求。</p>
                ) : (
                  <ul className="plain-list">
                    {stats.top_routes.map((item) => (
                      <li key={item.route}>
                        <code>{item.route}</code> × {formatNumber(item.count)}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          ) : (
            <LoadingState label="正在统计…" />
          )}
        </Card>

        <Card title="尚不可用的运维项" subtitle="以下内容没有可靠的数据来源，不会被估算或伪造">
          <ul className="plain-list">
            <li>
              <NotAvailable reason="服务端日志流（日志级别与模块筛选）尚未通过 API 暴露" />
            </li>
            <li>
              <NotAvailable reason="JME 同步与 MCE 索引同步的状态接口尚未实现" />
            </li>
            <li>
              <NotAvailable reason="记忆变更事件流（变更时间线）尚未实现，可用审计日志代替" />
            </li>
            <li>
              <NotAvailable reason="存储与索引的容量健康检查尚未实现" />
            </li>
          </ul>
        </Card>
      </div>

      <Card title="时区" subtitle="时间显示使用设置中的时区">
        <KeyValue items={[{ label: "当前时区", value: timezone }]} />
      </Card>
    </>
  );
}
