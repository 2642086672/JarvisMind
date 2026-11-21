import { useState, type FormEvent } from "react";
import { useConsole } from "../app/ConsoleContext";
import { describeError } from "../lib/api/client";
import { api, type SpaceCreateBody } from "../lib/api/endpoints";
import type { Space, SpaceDetail } from "../lib/api/types";
import { useAsync } from "../lib/hooks";
import { formatDateTime, formatNumber, statusTone } from "../lib/format";
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
  Notice,
  PageHeader,
  useToast,
} from "../components/ui";

const SPACE_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{1,62}$/;

const SPACE_TYPES: { id: string; label: string }[] = [
  { id: "personal", label: "个人空间" },
  { id: "work", label: "工作空间" },
  { id: "project", label: "项目空间" },
  { id: "shared", label: "共享空间" },
  { id: "custom", label: "自定义空间" },
];

const COUNT_LABELS: Record<string, string> = {
  active: "有效",
  archived: "归档",
  deleted: "已遗忘",
  merged: "已合并",
};

const ACCESS_LABELS: Record<string, string> = { read: "只读", readwrite: "读写" };

const EMPTY_DRAFT: SpaceCreateBody = { id: "", name: "", description: "", space_type: "custom" };

export function SpacesPage() {
  const { spaceId, setSpaceId, spacesVersion, bumpSpaces, timezone } = useConsole();
  const toast = useToast();
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<SpaceCreateBody>(EMPTY_DRAFT);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [editName, setEditName] = useState<string | null>(null);
  const [editDescription, setEditDescription] = useState<string | null>(null);

  const spaces = useAsync(() => api.listSpaces(includeArchived), [includeArchived, spacesVersion]);
  const detail = useAsync<{ data: SpaceDetail } | null>(
    () => (selectedId ? api.getSpace(selectedId) : Promise.resolve(null)),
    [selectedId, spacesVersion],
  );

  function refresh(): void {
    spaces.reload();
    detail.reload();
    bumpSpaces();
  }

  async function createSpace(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const id = (draft.id ?? "").trim();
    const name = (draft.name ?? "").trim();
    if (!SPACE_ID_PATTERN.test(id)) {
      setFormError("空间 ID 为 2–63 位小写字母、数字、连字符或下划线，且不能以符号开头。");
      return;
    }
    if (name.length === 0) {
      setFormError("请填写空间名称。");
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      const response = await api.createSpace({ ...draft, id, name });
      toast.notify(`已创建记忆空间：${response.data.name}`, "success");
      setDraft(EMPTY_DRAFT);
      setSelectedId(response.data.id);
      refresh();
    } catch (cause) {
      setFormError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  async function changeStatus(space: SpaceDetail, status: "active" | "archived"): Promise<void> {
    setBusy(true);
    setActionError(null);
    try {
      await api.updateSpace(space.id, { status });
      toast.notify(status === "archived" ? "空间已归档，Agent 将无法访问" : "空间已启用", "success");
      refresh();
    } catch (cause) {
      setActionError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  async function saveDetails(space: SpaceDetail): Promise<void> {
    setBusy(true);
    setActionError(null);
    try {
      await api.updateSpace(space.id, {
        name: editName ?? space.name,
        description: editDescription ?? space.description,
      });
      setEditName(null);
      setEditDescription(null);
      toast.notify("空间信息已保存", "success");
      refresh();
    } catch (cause) {
      setActionError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  const rows: Space[] = spaces.data?.data ?? [];
  const current = detail.data?.data;

  return (
    <>
      <PageHeader
        title="记忆空间"
        description="每个空间拥有独立的记忆数据库与向量索引。Agent 只能访问被授权的空间，跨空间读取会被服务端拒绝。"
      />
      <Notice tone="info" title="隔离方式">
        空间之间是物理隔离：每个非默认空间使用独立的数据库文件和向量目录。授权关系在「Agent 管理」中设置。
      </Notice>

      <div className="grid two">
        <Card
          title="空间列表"
          subtitle="点击一行查看详情；当前使用的空间会显示在顶部选择器中"
          actions={
            <label className="toolbar">
              <input type="checkbox" checked={includeArchived} onChange={(event) => setIncludeArchived(event.target.checked)} />
              <span className="small">显示已归档</span>
            </label>
          }
        >
          {spaces.error ? (
            <ErrorState error={spaces.error} onRetry={spaces.reload} />
          ) : (
            <DataTable
              caption="记忆空间列表"
              rows={rows}
              rowKey={(row) => row.id}
              onRowClick={(row) => setSelectedId(row.id)}
              empty={<EmptyState title="没有记忆空间" />}
              columns={[
                {
                  key: "name",
                  header: "名称",
                  render: (row) => (
                    <>
                      {row.name}
                      {row.is_default ? (
                        <>
                          {" "}
                          <Badge tone="info">默认</Badge>
                        </>
                      ) : null}
                      <div className="muted small">
                        <code>{row.id}</code>
                      </div>
                    </>
                  ),
                },
                { key: "type", header: "类型", render: (row) => row.space_type },
                { key: "status", header: "状态", render: (row) => <Badge tone={statusTone(row.status)}>{row.status === "active" ? "启用" : "已归档"}</Badge> },
                {
                  key: "current",
                  header: "当前",
                  render: (row) =>
                    row.id === spaceId ? (
                      <Badge tone="success">使用中</Badge>
                    ) : (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={row.status !== "active"}
                        onClick={(event) => {
                          event.stopPropagation();
                          setSpaceId(row.id);
                        }}
                      >
                        切换到此空间
                      </Button>
                    ),
                },
              ]}
            />
          )}
        </Card>

        <Card title="空间详情" subtitle={current ? `ID：${current.id}` : "在左侧选择一个空间"}>
          {selectedId === null ? (
            <EmptyState title="未选择空间" description="选择一个空间后，这里显示记忆数量、授权 Agent 与可执行的操作。" />
          ) : detail.error ? (
            <ErrorState error={detail.error} onRetry={detail.reload} />
          ) : !current ? (
            <LoadingState label="正在读取空间…" />
          ) : (
            <>
              {actionError ? <Notice tone="danger">{actionError}</Notice> : null}
              <KeyValue
                items={[
                  { label: "名称", value: current.name },
                  { label: "类型", value: SPACE_TYPES.find((item) => item.id === current.space_type)?.label ?? current.space_type },
                  { label: "状态", value: <Badge tone={statusTone(current.status)}>{current.status === "active" ? "启用" : "已归档"}</Badge> },
                  { label: "存储", value: current.provisioned ? "已初始化" : "未初始化" },
                  { label: "创建时间", value: formatDateTime(current.created_at, timezone) },
                  { label: "修改时间", value: formatDateTime(current.updated_at, timezone) },
                ]}
              />

              <h3 className="card-title">记忆数量</h3>
              {current.memory_counts ? (
                <KeyValue
                  items={Object.entries(COUNT_LABELS).map(([key, label]) => ({
                    label,
                    value: formatNumber(current.memory_counts?.[key] ?? 0),
                  }))}
                />
              ) : (
                <p className="muted small">该空间尚未初始化或已归档，无法统计。</p>
              )}

              <h3 className="card-title">已授权的 Agent</h3>
              {current.agents.length === 0 ? (
                <p className="muted small">没有 Agent 被授权访问此空间。</p>
              ) : (
                <ul className="plain-list">
                  {current.agents.map((agent) => (
                    <li key={agent.id}>
                      {agent.name} <Badge tone="info">{ACCESS_LABELS[agent.access] ?? agent.access}</Badge>
                    </li>
                  ))}
                </ul>
              )}

              {current.is_default ? (
                <Notice tone="neutral">默认空间是安装目录的根，不能归档。</Notice>
              ) : (
                <form
                  className="stack"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void saveDetails(current);
                  }}
                >
                  <Field label="名称" id="space-name">
                    <input
                      id="space-name"
                      className="input"
                      maxLength={120}
                      value={editName ?? current.name}
                      onChange={(event) => setEditName(event.target.value)}
                    />
                  </Field>
                  <Field label="描述" id="space-description">
                    <textarea
                      id="space-description"
                      className="textarea"
                      maxLength={500}
                      rows={3}
                      value={editDescription ?? current.description}
                      onChange={(event) => setEditDescription(event.target.value)}
                    />
                  </Field>
                  <div className="toolbar wrap">
                    <Button type="submit" variant="primary" loading={busy}>
                      保存信息
                    </Button>
                    {current.status === "active" ? (
                      <Button type="button" variant="danger" disabled={busy} onClick={() => void changeStatus(current, "archived")}>
                        归档空间
                      </Button>
                    ) : (
                      <Button type="button" disabled={busy} onClick={() => void changeStatus(current, "active")}>
                        重新启用
                      </Button>
                    )}
                  </div>
                </form>
              )}
            </>
          )}
        </Card>
      </div>

      <Card title="新建记忆空间" subtitle="创建时会先检查 ID，再初始化独立的数据库；失败不会留下残留存储">
        <form className="stack" onSubmit={(event) => void createSpace(event)}>
          {formError ? <Notice tone="danger">{formError}</Notice> : null}
          <div className="grid two">
            <Field label="空间 ID" id="new-space-id" hint="例如 work-2026。创建后不能修改。">
              <input
                id="new-space-id"
                className="input"
                maxLength={63}
                value={draft.id ?? ""}
                onChange={(event) => setDraft({ ...draft, id: event.target.value })}
              />
            </Field>
            <Field label="名称" id="new-space-name">
              <input
                id="new-space-name"
                className="input"
                maxLength={120}
                value={draft.name ?? ""}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              />
            </Field>
          </div>
          <Field label="类型" id="new-space-type">
            <select
              id="new-space-type"
              className="select"
              value={draft.space_type ?? "custom"}
              onChange={(event) => setDraft({ ...draft, space_type: event.target.value })}
            >
              {SPACE_TYPES.map((type) => (
                <option key={type.id} value={type.id}>
                  {type.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="描述（可选）" id="new-space-description">
            <textarea
              id="new-space-description"
              className="textarea"
              maxLength={500}
              rows={2}
              value={draft.description ?? ""}
              onChange={(event) => setDraft({ ...draft, description: event.target.value })}
            />
          </Field>
          <div>
            <Button type="submit" variant="primary" loading={busy}>
              创建空间
            </Button>
          </div>
        </form>
      </Card>
    </>
  );
}
