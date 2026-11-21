import { useState } from "react";
import { useConsole } from "../app/ConsoleContext";
import { describeError, downloadFile } from "../lib/api/client";
import { api } from "../lib/api/endpoints";
import type { BackupVerification } from "../lib/api/types";
import { useAsync } from "../lib/hooks";
import { formatBytes, formatDateTime } from "../lib/format";
import {
  Badge,
  Button,
  Card,
  DataTable,
  ErrorState,
  KeyValue,
  LoadingState,
  Modal,
  Notice,
  PageHeader,
  useToast,
} from "../components/ui";

export function BackupPage() {
  const { spaceId, timezone } = useConsole();
  const toast = useToast();
  const backups = useAsync(() => api.listBackups(), [spaceId]);
  const info = useAsync(() => api.systemInfo(), []);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [verification, setVerification] = useState<Record<string, BackupVerification>>({});
  const [sensitiveOpen, setSensitiveOpen] = useState(false);
  const [sensitiveTarget, setSensitiveTarget] = useState<"memories" | "audit" | null>(null);

  async function createBackup(): Promise<void> {
    setBusy("create");
    setError(null);
    try {
      const response = await api.createBackup();
      toast.notify(`已创建备份：${response.data.name}`, "success");
      backups.reload();
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(null);
    }
  }

  async function verify(name: string): Promise<void> {
    setBusy(name);
    setError(null);
    try {
      const response = await api.verifyBackup(name);
      setVerification((current) => ({ ...current, [name]: response.data }));
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(null);
    }
  }

  async function exportFile(kind: "memories-json" | "memories-csv" | "graph" | "audit", includeSensitive: boolean): Promise<void> {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "memories-json") {
        await downloadFile("/api/v1/console/exports/memories", { format: "json", include_sensitive: includeSensitive }, "jarvismind-memories.json");
      } else if (kind === "memories-csv") {
        await downloadFile("/api/v1/console/exports/memories", { format: "csv", include_sensitive: includeSensitive }, "jarvismind-memories.csv");
      } else if (kind === "graph") {
        await downloadFile("/api/v1/console/exports/graph", { include_sensitive: includeSensitive }, "jarvismind-graph.json");
      } else {
        await downloadFile("/api/v1/console/exports/audit", { include_sensitive: includeSensitive }, "jarvismind-audit.json");
      }
      toast.notify("导出已开始下载", "success");
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(null);
    }
  }

  function requestExport(kind: "memories-json" | "memories-csv" | "graph" | "audit"): void {
    void exportFile(kind, false);
  }

  function requestSensitiveExport(target: "memories" | "audit"): void {
    setSensitiveTarget(target);
    setSensitiveOpen(true);
  }

  const list = backups.data?.data ?? [];

  return (
    <>
      <PageHeader
        title="备份与导出"
        description="备份按记忆空间分别保存，并可验证完整性。导出默认对敏感内容脱敏；包含敏感内容必须明确确认。"
        actions={
          <Button variant="primary" loading={busy === "create"} onClick={() => void createBackup()}>
            创建备份
          </Button>
        }
      />
      {error ? <Notice tone="danger">{error}</Notice> : null}

      <Card title="备份列表" subtitle="只显示当前空间的备份；文件名格式为 jarvismind-时间戳.db">
        {backups.error ? (
          <ErrorState error={backups.error} onRetry={backups.reload} />
        ) : !backups.data ? (
          <LoadingState label="正在读取备份…" />
        ) : (
          <DataTable
            caption="备份列表"
            rows={list}
            rowKey={(row) => row.name}
            empty={<p className="muted small">当前空间还没有备份。</p>}
            columns={[
              { key: "name", header: "文件", render: (row) => <code>{row.name}</code> },
              { key: "size", header: "大小", render: (row) => formatBytes(row.size_bytes) },
              { key: "modified", header: "修改时间", render: (row) => formatDateTime(row.modified_at, timezone) },
              {
                key: "verify",
                header: "验证",
                render: (row) => {
                  const result = verification[row.name];
                  if (!result) return <span className="muted small">未验证</span>;
                  return result.ok ? (
                    <Badge tone="success">完整（{result.memory_records ?? 0} 条记忆）</Badge>
                  ) : (
                    <Badge tone="danger">{result.reason ?? result.integrity ?? "失败"}</Badge>
                  );
                },
              },
              {
                key: "actions",
                header: "操作",
                render: (row) => (
                  <Button size="sm" loading={busy === row.name} onClick={() => void verify(row.name)}>
                    验证完整性
                  </Button>
                ),
              },
            ]}
          />
        )}
        {backups.data ? (
          <Notice tone="warning" title="恢复">
            {backups.data.restore.procedure}
          </Notice>
        ) : null}
      </Card>

      <div className="grid two">
        <Card title="数据导出" subtitle="导出的文件由服务端生成，下载时携带所有者凭据">
          <div className="stack">
            <div className="toolbar wrap">
              <Button loading={busy === "memories-json"} onClick={() => requestExport("memories-json")}>
                记忆 JSON（已脱敏）
              </Button>
              <Button loading={busy === "memories-csv"} onClick={() => requestExport("memories-csv")}>
                记忆 CSV（已脱敏）
              </Button>
              <Button loading={busy === "graph"} onClick={() => requestExport("graph")}>
                图谱 JSON（已脱敏）
              </Button>
              <Button loading={busy === "audit"} onClick={() => requestExport("audit")}>
                审计 JSON（已脱敏）
              </Button>
            </div>
            <div className="toolbar wrap">
              <Button variant="danger" onClick={() => requestSensitiveExport("memories")}>
                包含敏感内容的记忆导出…
              </Button>
              <Button variant="danger" onClick={() => requestSensitiveExport("audit")}>
                包含敏感内容的审计导出…
              </Button>
            </div>
            <p className="muted small">导出中的记忆 ID、状态与类型可以用于迁移；敏感内容只有在明确确认后才会出现在文件中。</p>
          </div>
        </Card>

        <Card title="存储概况" subtitle="数据库路径不在界面中显示">
          {info.error ? (
            <ErrorState error={info.error} onRetry={info.reload} />
          ) : info.data ? (
            <KeyValue
              items={[
                { label: "数据库类型", value: info.data.database_type },
                { label: "当前空间", value: spaceId },
                { label: "备份方式", value: "VACUUM INTO（在线一致性快照）" },
                { label: "恢复方式", value: "仅离线，由运维人员执行" },
              ]}
            />
          ) : (
            <LoadingState label="正在读取…" />
          )}
        </Card>
      </div>

      <Modal
        open={sensitiveOpen}
        title="包含敏感内容的导出"
        onClose={() => setSensitiveOpen(false)}
        footer={
          <>
            <Button onClick={() => setSensitiveOpen(false)}>取消</Button>
            <Button
              variant="danger"
              onClick={() => {
                const target = sensitiveTarget;
                setSensitiveOpen(false);
                if (target === "memories") void exportFile("memories-json", true);
                if (target === "audit") void exportFile("audit", true);
              }}
            >
              我理解风险，继续导出
            </Button>
          </>
        }
      >
        <Notice tone="warning" title="请确认">
          导出文件会包含健康、住址、财务等敏感内容的原文。请只保存在可信设备上，并在使用后删除。这个操作会被记录在审计中。
        </Notice>
      </Modal>
    </>
  );
}
