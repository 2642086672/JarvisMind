import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useConsole } from "../app/ConsoleContext";
import { describeError } from "../lib/api/client";
import { api } from "../lib/api/endpoints";
import { MEMORY_TYPES, type MemoryStatus, type MemoryType, type PageMeta } from "../lib/api/types";
import { useAsync } from "../lib/hooks";
import { formatDateTime, memoryStatusLabel, memoryTypeLabel, statusTone, truncate } from "../lib/format";
import {
  Badge,
  Button,
  Card,
  DataTable,
  ErrorState,
  Field,
  Modal,
  NotAvailable,
  Notice,
  PageHeader,
  Pagination,
  Tabs,
  useToast,
} from "../components/ui";

const PAGE_SIZE = 20;

const STATUS_TABS: { id: MemoryStatus; label: string }[] = [
  { id: "ACTIVE", label: "有效" },
  { id: "ARCHIVED", label: "归档" },
  { id: "DELETED", label: "已遗忘" },
  { id: "MERGED", label: "已合并" },
];

const RECEIPT_LABELS: Record<string, string> = {
  SAVED: "已保存",
  UPDATED: "已更新",
  MERGED: "已合并",
  PENDING: "已进入待确认候选",
  PENDING_AUTHORIZATION: "敏感内容，等待所有者授权",
  DUPLICATE: "与已有记忆重复",
  CONFLICT: "与已有记忆冲突，等待处理",
  NO_INTENT: "没有识别到记忆请求",
  NO_MEMORY: "不是可保存的事实",
  DENIED: "凭据类内容已被拦截",
  FAILED: "保存失败，请稍后重试",
};

interface MemoryRow {
  id: string;
  content: string;
  memory_type: MemoryType;
  status: string;
  importance: number;
  created_at: string;
  rank: number | null;
}

interface MemoryPage {
  rows: MemoryRow[];
  meta: PageMeta;
  mode: "list" | "search";
}

export function MemoriesPage() {
  const { spaceId, timezone } = useConsole();
  const toast = useToast();
  const [status, setStatus] = useState<MemoryStatus>("ACTIVE");
  const [typeFilter, setTypeFilter] = useState<MemoryType | "">("");
  const [sortBy, setSortBy] = useState<"created_at" | "updated_at" | "importance">("created_at");
  const [page, setPage] = useState(1);
  const [draftQuery, setDraftQuery] = useState("");
  const [query, setQuery] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [createText, setCreateText] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const memories = useAsync<MemoryPage>(async () => {
    const trimmed = query.trim();
    if (trimmed.length > 0) {
      const result = await api.searchMemories({
        q: trimmed,
        status,
        memory_type: typeFilter || undefined,
        page,
        page_size: PAGE_SIZE,
      });
      return {
        mode: "search",
        meta: result.meta,
        rows: result.data.map((hit) => ({
          id: hit.memory_id,
          content: hit.content,
          memory_type: hit.memory_type,
          status,
          importance: hit.importance,
          created_at: hit.created_at,
          rank: hit.rank,
        })),
      };
    }
    const result = await api.listMemories({
      status,
      memory_type: typeFilter || undefined,
      page,
      page_size: PAGE_SIZE,
      sort_by: sortBy,
      sort_order: "desc",
    });
    return {
      mode: "list",
      meta: result.meta,
      rows: result.data.map((memory) => ({
        id: memory.id,
        content: memory.content,
        memory_type: memory.memory_type,
        status: memory.status,
        importance: memory.importance,
        created_at: memory.created_at,
        rank: null,
      })),
    };
  }, [spaceId, status, typeFilter, sortBy, page, query]);

  function submitSearch(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setPage(1);
    setQuery(draftQuery.trim());
  }

  async function submitCreate(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const text = createText.trim();
    if (text.length === 0) {
      setCreateError("请输入要记住的内容。");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const response = await api.autoRemember({
        text,
        conversation_id: `console-${new Date().toISOString().slice(0, 10)}`,
      });
      const receipt = response.data;
      const label = RECEIPT_LABELS[receipt.status] ?? receipt.status;
      toast.notify(`${label}：${receipt.message || ""}`.trim(), receipt.status === "DENIED" || receipt.status === "FAILED" ? "danger" : "success");
      setCreateText("");
      setCreateOpen(false);
      memories.reload();
    } catch (cause) {
      setCreateError(describeError(cause));
    } finally {
      setCreating(false);
    }
  }

  const data = memories.data;

  return (
    <>
      <PageHeader
        title="记忆浏览器"
        description="浏览、搜索并管理当前空间的记忆。新记忆经过自动记忆治理，凭据会被拦截，敏感内容会等待授权。"
        actions={
          <Button variant="primary" onClick={() => setCreateOpen(true)}>
            新建记忆
          </Button>
        }
      />

      <Card>
        <div className="toolbar">
          <Tabs label="记忆状态" items={STATUS_TABS} value={status} onChange={(id) => { setStatus(id as MemoryStatus); setPage(1); }} />
        </div>
        <form className="toolbar wrap" onSubmit={submitSearch} role="search">
          <label className="grow">
            <span className="sr-only">搜索记忆</span>
            <input
              className="input"
              type="search"
              placeholder="关键词搜索（服务端全文检索）"
              value={draftQuery}
              onChange={(event) => setDraftQuery(event.target.value)}
              maxLength={500}
            />
          </label>
          <Button type="submit" variant="secondary">
            搜索
          </Button>
          <label>
            <span className="sr-only">记忆类型</span>
            <select
              className="select"
              value={typeFilter}
              onChange={(event) => {
                setTypeFilter(event.target.value as MemoryType | "");
                setPage(1);
              }}
            >
              <option value="">全部类型</option>
              {MEMORY_TYPES.map((type) => (
                <option key={type} value={type}>
                  {memoryTypeLabel(type)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">排序</span>
            <select
              className="select"
              value={sortBy}
              disabled={query.length > 0}
              onChange={(event) => setSortBy(event.target.value as "created_at" | "updated_at" | "importance")}
            >
              <option value="created_at">最近创建</option>
              <option value="updated_at">最近修改</option>
              <option value="importance">重要性</option>
            </select>
          </label>
        </form>
        <p className="muted small">
          标签、时间范围、敏感度与来源过滤：<NotAvailable reason="服务端列表接口尚未提供这些过滤条件" />
        </p>
        {query.length > 0 ? (
          <p className="muted small">
            搜索结果按相关度排序；清空搜索框即可回到列表。当前搜索：“{query}”
          </p>
        ) : null}
      </Card>

      {memories.error ? <ErrorState error={memories.error} onRetry={memories.reload} /> : null}
      {!memories.error && data === undefined ? <p className="muted">正在读取记忆…</p> : null}
      {data ? (
        <Card title={`${memoryStatusLabel(status)}记忆`} subtitle={`共 ${data.meta.total} 条`}>
          <DataTable
            caption="记忆列表"
            rows={data.rows}
            rowKey={(row) => row.id}
            empty={
              <Notice tone="info" title={query.length > 0 ? "没有匹配的记忆" : "这里还没有记忆"}>
                {query.length > 0 ? "换一个关键词，或切换记忆状态。" : "可以点击右上角“新建记忆”，内容会经过自动记忆治理。"}
              </Notice>
            }
            columns={[
              {
                key: "content",
                header: "内容",
                render: (row) => (
                  <Link to={`/memories/${encodeURIComponent(row.id)}`}>{truncate(row.content, 120)}</Link>
                ),
              },
              { key: "type", header: "类型", render: (row) => memoryTypeLabel(row.memory_type) },
              {
                key: "status",
                header: "状态",
                render: (row) => <Badge tone={statusTone(row.status)}>{memoryStatusLabel(row.status)}</Badge>,
              },
              { key: "importance", header: "重要性", render: (row) => row.importance.toFixed(1) },
              { key: "created", header: "创建时间", render: (row) => formatDateTime(row.created_at, timezone) },
              {
                key: "rank",
                header: "相关度",
                render: (row) => (row.rank === null ? "—" : row.rank.toFixed(3)),
              },
            ]}
          />
          <Pagination page={page} totalPages={data.meta.total_pages} onPageChange={setPage} />
        </Card>
      ) : null}

      <Modal
        open={createOpen}
        title="新建记忆"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <Button onClick={() => setCreateOpen(false)}>取消</Button>
            <Button variant="primary" type="submit" form="create-memory-form" loading={creating}>
              提交
            </Button>
          </>
        }
      >
        <form id="create-memory-form" className="stack" onSubmit={submitCreate}>
          <Field label="要记住的内容" id="create-memory-text" hint="请用陈述句描述一个事实，例如“我喜欢在周末阅读科幻小说”。">
            <textarea
              id="create-memory-text"
              className="textarea"
              rows={5}
              maxLength={20000}
              value={createText}
              onChange={(event) => setCreateText(event.target.value)}
            />
          </Field>
          {createError ? <Notice tone="danger">{createError}</Notice> : null}
          <Notice tone="info">
            提交后由服务端判断是否保存：凭据会被拦截，敏感内容等待所有者授权，其它内容按治理策略入库或进入候选。
          </Notice>
        </form>
      </Modal>
    </>
  );
}
