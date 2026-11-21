import { useState } from "react";
import { useConsole } from "../app/ConsoleContext";
import { describeError, downloadFile } from "../lib/api/client";
import { api } from "../lib/api/endpoints";
import type { GraphEdge, GraphNode, JkgNeighbor, JkgOverview, JkgTraversal } from "../lib/api/types";
import { useAsync } from "../lib/hooks";
import { formatNumber, truncate } from "../lib/format";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  KeyValue,
  LoadingState,
  NotAvailable,
  Notice,
  PageHeader,
} from "../components/ui";

interface Point {
  x: number;
  y: number;
}

function ringLayout(center: string, first: string[], second: string[]): Map<string, Point> {
  const positions = new Map<string, Point>();
  positions.set(center, { x: 0, y: 0 });
  const place = (ids: string[], radius: number) => {
    ids.forEach((id, index) => {
      const angle = (2 * Math.PI * index) / Math.max(1, ids.length) - Math.PI / 2;
      positions.set(id, { x: Math.round(radius * Math.cos(angle)), y: Math.round(radius * Math.sin(angle)) });
    });
  };
  place(first, 120);
  place(second, 215);
  return positions;
}

function GraphCanvas({
  centerId,
  neighbors,
  traversal,
  onSelect,
}: {
  centerId: string;
  neighbors: JkgNeighbor[];
  traversal: JkgTraversal | undefined;
  onSelect: (nodeId: string) => void;
}) {
  const names = new Map<string, string>();
  const firstRing: string[] = [];
  for (const entry of neighbors) {
    if (entry.node.node_id === centerId || firstRing.includes(entry.node.node_id)) continue;
    firstRing.push(entry.node.node_id);
    names.set(entry.node.node_id, entry.node.display_name || entry.node.name);
  }
  const secondRing: string[] = [];
  for (const entry of traversal?.nodes ?? []) {
    if (entry.hop !== 2) continue;
    const id = entry.node.node_id;
    if (id === centerId || firstRing.includes(id) || secondRing.includes(id)) continue;
    secondRing.push(id);
    names.set(id, entry.node.display_name || entry.node.name);
  }
  const visibleFirst = firstRing.slice(0, 16);
  const visibleSecond = secondRing.slice(0, 16);
  const positions = ringLayout(centerId, visibleFirst, visibleSecond);
  const edges = neighbors.filter((entry) => positions.has(entry.node.node_id));

  return (
    <svg
      className="graph-canvas"
      viewBox="-260 -260 520 520"
      role="img"
      aria-label="选中节点及其关联节点的关系图（点击节点可查看详情）"
    >
      {edges.map((entry) => {
        const from = positions.get(entry.direction === "outgoing" ? centerId : entry.node.node_id);
        const to = positions.get(entry.direction === "outgoing" ? entry.node.node_id : centerId);
        if (!from || !to) return null;
        return (
          <line
            key={`line-${entry.edge.edge_id}`}
            x1={from.x}
            y1={from.y}
            x2={to.x}
            y2={to.y}
            className={entry.edge.is_valid ? "graph-edge" : "graph-edge graph-edge-invalid"}
          />
        );
      })}
      {[...visibleFirst, ...visibleSecond].map((id) => {
        const point = positions.get(id);
        if (!point) return null;
        return (
          <g
            key={id}
            className="graph-node"
            role="button"
            tabIndex={0}
            onClick={() => onSelect(id)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") onSelect(id);
            }}
          >
            <circle cx={point.x} cy={point.y} r={18} />
            <text x={point.x} y={point.y + 34} textAnchor="middle">
              {truncate(names.get(id) ?? id, 12)}
            </text>
          </g>
        );
      })}
      <g className="graph-node graph-node-center">
        <circle cx={0} cy={0} r={24} />
        <text x={0} y={4} textAnchor="middle">
          中心
        </text>
      </g>
    </svg>
  );
}

export function GraphPage() {
  const { spaceId } = useConsole();
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const status = useAsync(() => api.graphStatus(), [spaceId]);
  const usable = status.data?.usable === true;
  const overview = useAsync<JkgOverview | null>(
    () => (usable ? api.graphOverview() : Promise.resolve(null)),
    [spaceId, usable],
  );
  const nodes = useAsync(
    () => api.graphNodes({ q: query || undefined, page, page_size: 20 }),
    [spaceId, usable, query, page],
  );
  const detail = useAsync(
    () => (selected && usable ? api.graphNode(selected) : Promise.resolve(null)),
    [spaceId, selected, usable],
  );
  const neighbors = useAsync(
    () => (selected && usable ? api.graphNeighbors(selected, { direction: "both", valid_only: true, page_size: 50 }) : Promise.resolve(null)),
    [spaceId, selected, usable],
  );
  const traversal = useAsync(
    () => (selected && usable ? api.graphTraverse(selected, { hops: 2, direction: "both", max_nodes: 50, max_edges: 100 }) : Promise.resolve(null)),
    [spaceId, selected, usable],
  );
  const sync = useAsync(() => api.graphSyncStatus(), [spaceId]);

  async function exportJson(): Promise<void> {
    setExporting(true);
    setExportError(null);
    try {
      await downloadFile("/api/v1/console/exports/graph", { include_sensitive: false }, "jarvismind-graph.json");
    } catch (cause) {
      setExportError(describeError(cause));
    } finally {
      setExporting(false);
    }
  }

  if (status.error) return <ErrorState error={status.error} onRetry={status.reload} />;
  if (!status.data) return <LoadingState label="正在读取图谱状态…" />;

  if (!usable) {
    return (
      <>
        <PageHeader title="知识图谱" description="图谱是由记忆派生的视图，不是独立的记忆存储。" />
        <Card title="图谱不可用">
          <p>
            <Badge tone="warning">{status.data.state}</Badge> 图谱的启用开关或表结构尚未就绪，因此这里不显示任何图谱数据。
          </p>
          {status.data.missing_tables.length > 0 ? (
            <p className="muted small">缺少的表：{status.data.missing_tables.join("、")}</p>
          ) : null}
          <Notice tone="info" title="如何启用">
            在服务端设置 jkg.enabled，并按部署文档安装图谱表结构（见 docs/KNOWLEDGE-GRAPH.md）。这一步不会修改记忆数据。
          </Notice>
        </Card>
      </>
    );
  }

  const selectedDetail = detail.data?.data;
  const neighborItems = neighbors.data?.data ?? [];
  const nodeRows = nodes.data?.data ?? [];

  return (
    <>
      <PageHeader
        title="知识图谱"
        description="从节点出发探索关联。图谱中的节点与关系由记忆派生；记忆被遗忘后，相关节点会标记为来源已失效，并不再返回给 Agent。"
        actions={
          <Button size="sm" loading={exporting} onClick={() => void exportJson()}>
            导出 JSON
          </Button>
        }
      />
      {exportError ? <Notice tone="danger">{exportError}</Notice> : null}

      <Card title="图谱概览">
        {overview.error ? (
          <ErrorState error={overview.error} onRetry={overview.reload} />
        ) : overview.data ? (
          <>
            <KeyValue
              items={[
                { label: "节点", value: formatNumber(overview.data.counts.nodes) },
                { label: "关系", value: formatNumber(overview.data.counts.edges) },
                { label: "来源记录", value: formatNumber(overview.data.counts.sources) },
                { label: "已失效来源", value: formatNumber(overview.data.counts.invalidated_sources) },
                {
                  label: "无效关系（扫描窗口内）",
                  value: formatNumber(overview.data.invalid_edges),
                },
                {
                  label: "孤立关系",
                  value: formatNumber(overview.data.orphan_edges.length),
                },
              ]}
            />
            {overview.data.truncated ? (
              <p className="muted small">无效关系的统计只覆盖了扫描窗口（{formatNumber(overview.data.scanned_edges)} 条），不是全表结果。</p>
            ) : null}
          </>
        ) : (
          <LoadingState label="正在统计…" />
        )}
        <ul className="plain-list">
          <li>
            <NotAvailable reason="重复节点、重复关系、未解析关系与抽取错误的专项统计尚未由服务端提供" />
          </li>
          <li>
            <NotAvailable reason="规则软失效的预览与执行接口尚未实现" />
          </li>
          <li>
            <NotAvailable reason="图谱截图导出；当前只支持 JSON 数据导出" />
          </li>
        </ul>
      </Card>

      <div className="grid graph-layout">
        <Card title="节点">
          <form
            className="toolbar"
            onSubmit={(event) => {
              event.preventDefault();
              setPage(1);
              setQuery(draft.trim());
            }}
          >
            <label className="grow">
              <span className="sr-only">搜索节点</span>
              <input className="input" type="search" value={draft} placeholder="按名称搜索" maxLength={200} onChange={(event) => setDraft(event.target.value)} />
            </label>
            <Button type="submit" size="sm">
              搜索
            </Button>
          </form>
          {nodes.error ? (
            <ErrorState error={nodes.error} onRetry={nodes.reload} />
          ) : (
            <DataTable
              caption="图谱节点"
              rows={nodeRows}
              rowKey={(row: GraphNode) => row.node_id}
              onRowClick={(row) => setSelected(row.node_id)}
              empty={<EmptyState title="没有节点" description="图谱为空，或搜索条件没有匹配。" />}
              columns={[
                { key: "name", header: "名称", render: (row) => row.display_name || row.name },
                { key: "type", header: "类型", render: (row) => row.entity_type },
                {
                  key: "valid",
                  header: "来源",
                  render: (row) =>
                    row.source_memory_valid ? (
                      <Badge tone="success">有效</Badge>
                    ) : (
                      <Badge tone="danger">已失效</Badge>
                    ),
                },
              ]}
            />
          )}
          {nodes.data && nodes.data.meta.total_pages && nodes.data.meta.total_pages > 1 ? (
            <div className="toolbar">
              <Button size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                上一页
              </Button>
              <span className="muted small">第 {page} 页</span>
              <Button size="sm" disabled={!nodes.data.meta.total || page * 20 >= nodes.data.meta.total} onClick={() => setPage(page + 1)}>
                下一页
              </Button>
            </div>
          ) : null}
        </Card>

        <Card title="关系视图" subtitle={selected ? "中心节点及其一度、二度关联" : "在左侧选择一个节点"}>
          {selected === null ? (
            <EmptyState title="未选择节点" description="选择节点后，这里显示它的关系网络。" />
          ) : detail.loading && !selectedDetail ? (
            <LoadingState label="正在读取节点…" />
          ) : detail.error ? (
            <ErrorState error={detail.error} onRetry={detail.reload} />
          ) : (
            <>
              <GraphCanvas
                centerId={selected}
                neighbors={neighborItems}
                traversal={traversal.data?.data}
                onSelect={(nodeId) => setSelected(nodeId)}
              />
              {selectedDetail ? (
                <KeyValue
                  items={[
                    { label: "名称", value: selectedDetail.node.display_name || selectedDetail.node.name },
                    { label: "类型", value: selectedDetail.node.entity_type },
                    {
                      label: "来源记忆",
                      value: selectedDetail.node.source_memory_valid ? "有效" : "已失效（不会返回给 Agent）",
                    },
                    { label: "出边 / 入边", value: `${selectedDetail.outgoing_edges.length} / ${selectedDetail.incoming_edges.length}` },
                    {
                      label: "关系总数",
                      value: selectedDetail.edge_total === null ? "超出抓取窗口" : formatNumber(selectedDetail.edge_total),
                    },
                  ]}
                />
              ) : null}
              {traversal.data?.data.truncated ? (
                <p className="muted small">遍历达到了上限（节点 {traversal.data.data.limits.max_nodes}，关系 {traversal.data.data.limits.max_edges}），结果不完整。</p>
              ) : null}
            </>
          )}
        </Card>
      </div>

      {selectedDetail ? (
        <Card title="关系列表" subtitle="只显示关系的类型、方向与可信度；来源详情可在 /api/v1/jkg/edges/{id} 查看">
          <DataTable
            caption="中心节点的关系"
            rows={[
              ...selectedDetail.outgoing_edges.map((edge: GraphEdge) => ({ edge, direction: "出" as const })),
              ...selectedDetail.incoming_edges.map((edge: GraphEdge) => ({ edge, direction: "入" as const })),
            ]}
            rowKey={(row) => `${row.direction}-${row.edge.edge_id}`}
            columns={[
              { key: "direction", header: "方向", render: (row) => row.direction },
              { key: "predicate", header: "关系", render: (row) => row.edge.predicate },
              { key: "confidence", header: "可信度", render: (row) => row.edge.confidence.toFixed(2) },
              {
                key: "valid",
                header: "有效性",
                render: (row) => (row.edge.is_valid ? <Badge tone="success">有效</Badge> : <Badge tone="danger">无效</Badge>),
              },
              { key: "sources", header: "来源数", render: (row) => `${row.edge.valid_source_count}/${row.edge.source_count}` },
            ]}
          />
        </Card>
      ) : null}

      <Card title="图谱同步状态" subtitle="同步由服务端单次执行，不会在后台自动运行">
        {sync.error ? (
          <ErrorState error={sync.error} onRetry={sync.reload} />
        ) : (
          <pre className="code-block">{JSON.stringify(sync.data ?? {}, null, 2)}</pre>
        )}
      </Card>
    </>
  );
}
