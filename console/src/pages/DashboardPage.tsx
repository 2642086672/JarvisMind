import { Link } from "react-router-dom";
import { useConsole } from "../app/ConsoleContext";
import { api } from "../lib/api/endpoints";
import { useAsync } from "../lib/hooks";
import {
  formatDateTime,
  formatDuration,
  formatNumber,
  memoryTypeLabel,
  statusTone,
} from "../lib/format";
import {
  Badge,
  BarChart,
  Button,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  HorizontalBars,
  LoadingState,
  NotAvailable,
  PageHeader,
  StatCard,
} from "../components/ui";

function graphReason(status: string): string {
  if (status === "disabled") return "图谱已关闭（jkg.enabled 未开启）";
  if (status === "schema_not_ready") return "图谱表结构尚未安装";
  return `图谱不可用（状态：${status}）`;
}

export function DashboardPage() {
  const { spaceId, timezone } = useConsole();
  const state = useAsync(() => api.dashboard(), [spaceId]);

  if (state.error) return <ErrorState error={state.error} onRetry={state.reload} />;
  const data = state.data?.data;
  if (!data) return <LoadingState label="正在读取仪表盘…" />;

  const pending = data.pending;
  const pendingTotal =
    pending.candidates + pending.sensitive_authorization + pending.forget_requests + pending.modification_requests;

  return (
    <>
      <PageHeader
        title="仪表盘"
        description={`空间 ${spaceId} 的记忆、图谱与系统状态。数字全部来自服务端；服务端不提供的指标会标注为 Not Available。`}
        actions={
          <Button size="sm" onClick={state.reload}>
            刷新
          </Button>
        }
      />

      <section className="grid stats" aria-label="记忆统计">
        <StatCard label="记忆总数" value={formatNumber(data.memory.total)} hint={`有效 ${formatNumber(data.memory.active)} 条`} />
        <StatCard label="有效记忆" value={formatNumber(data.memory.active)} tone="success" />
        <StatCard label="归档记忆" value={formatNumber(data.memory.archived)} tone="warning" />
        <StatCard label="已遗忘" value={formatNumber(data.memory.deleted)} tone="danger" />
        <StatCard label="今日新增" value={formatNumber(data.memory.today_created)} />
        <StatCard label="今日修改" value={formatNumber(data.memory.today_updated)} />
        <StatCard
          label="待治理"
          value={formatNumber(pendingTotal)}
          hint="候选、待授权与待审批请求"
          tone={pendingTotal > 0 ? "warning" : "neutral"}
        />
      </section>

      <div className="grid two">
        <Card title="记忆增长趋势" subtitle="最近 14 天新增记忆数量">
          <BarChart
            ariaLabel="最近 14 天新增记忆数量"
            data={data.growth.map((item) => ({ label: item.day.slice(5), value: item.count }))}
          />
        </Card>

        <Card title="记忆类型分布">
          <HorizontalBars
            ariaLabel="记忆类型分布"
            data={data.distribution.types.map((item) => ({ label: memoryTypeLabel(item.type), value: item.count }))}
          />
        </Card>

        <Card title="记忆来源分布">
          <HorizontalBars
            ariaLabel="记忆来源分布"
            data={data.distribution.sources.map((item) => ({ label: item.source, value: item.count }))}
          />
        </Card>

        <Card
          title="知识图谱"
          actions={
            <Link className="btn btn-ghost btn-sm" to="/graph">
              打开图谱
            </Link>
          }
        >
          {data.graph.status === "ready" && data.graph.counts ? (
            <dl className="kv">
              <div className="kv-row">
                <dt>节点</dt>
                <dd>{formatNumber(data.graph.counts.nodes)}</dd>
              </div>
              <div className="kv-row">
                <dt>关系</dt>
                <dd>{formatNumber(data.graph.counts.edges)}</dd>
              </div>
              <div className="kv-row">
                <dt>来源</dt>
                <dd>{formatNumber(data.graph.counts.sources)}</dd>
              </div>
              <div className="kv-row">
                <dt>无效关系（扫描窗口内）</dt>
                <dd>{formatNumber(data.graph.invalid_edges ?? 0)}</dd>
              </div>
              <div className="kv-row">
                <dt>孤立关系</dt>
                <dd>{formatNumber(data.graph.orphan_edges ?? 0)}</dd>
              </div>
              {data.graph.scan_truncated ? (
                <div className="kv-row">
                  <dt>说明</dt>
                  <dd>无效关系数量只统计了扫描窗口，不是全表结果。</dd>
                </div>
              ) : null}
            </dl>
          ) : (
            <p>
              <NotAvailable reason={graphReason(data.graph.status)} />
            </p>
          )}
        </Card>

        <Card title="待治理事项">
          <dl className="kv">
            <div className="kv-row">
              <dt>待确认候选</dt>
              <dd>{formatNumber(pending.candidates)}</dd>
            </div>
            <div className="kv-row">
              <dt>待授权敏感记忆</dt>
              <dd>{formatNumber(pending.sensitive_authorization)}</dd>
            </div>
            <div className="kv-row">
              <dt>待审批遗忘请求</dt>
              <dd>{formatNumber(pending.forget_requests)}</dd>
            </div>
            <div className="kv-row">
              <dt>待审批修改请求</dt>
              <dd>{formatNumber(pending.modification_requests)}</dd>
            </div>
          </dl>
          <p className="muted">
            <Link to="/governance">进入治理中心 →</Link>
          </p>
        </Card>

        <Card title="系统组件">
          {data.health.length === 0 ? (
            <EmptyState title="没有组件状态" />
          ) : (
            <DataTable
              caption="系统组件状态"
              columns={[
                { key: "component", header: "组件", render: (row) => row.component },
                {
                  key: "status",
                  header: "状态",
                  render: (row) => <Badge tone={statusTone(row.status)}>{row.status}</Badge>,
                },
                {
                  key: "detail",
                  header: "详情",
                  render: (row) =>
                    row.seconds !== undefined ? formatDuration(row.seconds) : row.version ?? "",
                },
              ]}
              rows={data.health}
              rowKey={(row) => row.component}
            />
          )}
        </Card>

        <Card title="最近活动" subtitle="记忆变更与 API 调用（仅元数据）">
          {data.activity.length === 0 ? (
            <EmptyState title="暂无活动" />
          ) : (
            <ul className="activity">
              {data.activity.map((item, index) => (
                <li key={`${item.at}-${index}`}>
                  {item.type === "memory" ? (
                    <>
                      <Badge tone="info">记忆</Badge> {item.action}
                      {item.memory_id ? (
                        <>
                          {" "}
                          <Link to={`/memories/${encodeURIComponent(item.memory_id)}`}>查看</Link>
                        </>
                      ) : null}
                    </>
                  ) : (
                    <>
                      <Badge tone={item.status_code >= 400 ? "warning" : "neutral"}>API {item.status_code}</Badge>{" "}
                      {item.action}
                    </>
                  )}
                  <time className="muted"> {formatDateTime(item.at, timezone)}</time>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="暂不可用的指标" subtitle="这些数字目前没有可靠的数据来源，不会被估算">
          <ul className="plain-list">
            {data.not_available.map((reason) => (
              <li key={reason}>
                <NotAvailable reason={reason} />
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </>
  );
}
