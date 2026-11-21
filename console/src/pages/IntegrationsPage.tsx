import { Badge, Card, DataTable, PageHeader, Notice } from "../components/ui";

interface Template {
  id: string;
  name: string;
  status: "verified" | "template";
  note: string;
}

const TEMPLATES: Template[] = [
  {
    id: "dsh",
    name: "DSH",
    status: "verified",
    note: "完整示例：Pre-Chat 召回、Post-Chat 记忆提交、跨会话连续性演示（Python 与 TypeScript）。",
  },
  { id: "deepseek", name: "DeepSeek", status: "template", note: "通用 HTTP 模板：对话前调用 /agent/context，对话后调用 /agent/remember。" },
  { id: "openai", name: "OpenAI 兼容接口", status: "template", note: "同上；适用于任何兼容 Chat Completions 的服务。" },
  { id: "claude", name: "Claude", status: "template", note: "同上；把召回的上下文放入系统提示，并按资料对待。" },
  { id: "openclaw", name: "OpenClaw", status: "template", note: "同上；使用 Agent 令牌与空间请求头。" },
  { id: "langchain", name: "LangChain", status: "template", note: "可封装为检索器（Retriever）与记忆写入工具。" },
  { id: "llamaindex", name: "LlamaIndex", status: "template", note: "可封装为自定义检索器与节点后处理器。" },
  { id: "custom", name: "自定义 Agent", status: "template", note: "任何能发送 HTTP 请求的程序都可以接入，参见接入文档。" },
];

const DOCS = [
  { path: "sdk/python/", description: "Python SDK（标准库实现，无第三方依赖）" },
  { path: "sdk/typescript/", description: "TypeScript SDK（基于 fetch，可在 Node 18+ 与浏览器中运行）" },
  { path: "docs/tutorials/INTEGRATE-WITH-DSH.md", description: "DSH 接入分步教程" },
  { path: "examples/dsh-integration/", description: "DSH 可运行示例与跨会话演示脚本" },
  { path: "docs/AGENT-INTEGRATION.md", description: "Agent 接入规范：令牌、权限、空间与提示注入安全" },
];

export function IntegrationsPage() {
  return (
    <>
      <PageHeader
        title="SDK 与接入"
        description="JarvisMind 不依赖任何特定 AI 应用。任何能发送 HTTP 请求的 Agent 都可以通过 Agent 令牌接入。"
      />

      <Notice tone="info" title="接入顺序">
        1. 在「Agent 管理」创建 Agent 并颁发令牌；2. 授予它所需的空间与权限；3. 对话前调用 <code>/api/v1/agent/context</code>；
        4. 对话后调用 <code>/api/v1/agent/remember</code>；5. 遗忘请求由所有者在治理中心批准。
      </Notice>

      <Card title="SDK 与文档">
        <DataTable
          caption="SDK 与文档位置"
          rows={DOCS}
          rowKey={(row) => row.path}
          columns={[
            { key: "path", header: "位置", render: (row) => <code>{row.path}</code> },
            { key: "description", header: "说明", render: (row) => row.description },
          ]}
        />
      </Card>

      <Card title="接入模板" subtitle="状态如实标注：只有 DSH 示例经过了端到端运行验证，其它为通用模板">
        <DataTable
          caption="接入模板"
          rows={TEMPLATES}
          rowKey={(row) => row.id}
          columns={[
            { key: "name", header: "应用", render: (row) => row.name },
            {
              key: "status",
              header: "状态",
              render: (row) =>
                row.status === "verified" ? <Badge tone="success">已端到端验证</Badge> : <Badge tone="neutral">通用模板（未单独验证）</Badge>,
            },
            { key: "note", header: "说明", render: (row) => row.note },
          ]}
        />
      </Card>

      <Card title="最小调用示例" subtitle="把 AGENT_TOKEN 替换为控制台颁发的令牌">
        <pre className="code-block">{`curl -s -X POST http://127.0.0.1:8777/api/v1/agent/context \\
  -H "Authorization: Bearer $AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"query": "我之前准备买什么电脑？", "limit": 5}'`}</pre>
      </Card>
    </>
  );
}
