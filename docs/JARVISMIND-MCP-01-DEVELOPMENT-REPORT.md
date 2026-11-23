# JARVISMIND-MCP-01 开发报告

| 项目 | 内容 |
| --- | --- |
| 任务编号 | `JARVISMIND-MCP-01` |
| 目标仓库 | JarvisMind 独立开源仓（分支 `main`） |
| 基础提交 | `a8f6ee2` |
| 功能提交 | `2cab085 feat(mcp): add native stdio MCP server (python -m jarvismind mcp)` |
| 报告提交 | `docs(mcp): add JARVISMIND-MCP-01 development report`（随附提交） |
| 远端状态 | `origin/main` 已包含功能提交 `2cab085031508f37c5e25d561b1b9b70a104c515`；本报告以随附 docs 提交追加 |
| Tag | 未打 `v1.0.0` |
| 报告日期 | 2026-11-23 |
| 结论 | **完成并通过全量门禁与独立只读审查** |

---

## 1. 执行摘要

本任务为 JarvisMind 增加了内置的原生 stdio MCP Server，入口：

```bash
python -m jarvismind mcp
```

它作为运行中的 JarvisMind HTTP 服务的轻量桥接客户端，只调用 `/api/v1/agent/*`，不直连 SQLite/Qdrant，不 import FastAPI/SQLAlchemy，不修改数据库 schema，不新增第三方依赖。Claude Desktop、Cursor、OpenCode、Windsurf、DSH 等 MCP 客户端可以通过一行配置直接接入统一记忆底座。

最终验证结果：

| 验证项 | 结果 |
| --- | --- |
| 后端全量 pytest | **2566 passed / 0 failed / 0 skipped** |
| P0 门禁 | **67/67**，AST 档1 违规 0 |
| 新增 MCP 测试 | **33 passed** |
| 真实端到端冒烟 | **E2E OK** |
| Markdown 相对链接 | 37 个文件 / 153 条 / **0 断链** |
| 真实令牌泄露 | **0** |
| 独立只读审查 | **PASS**（Qwen4 Max Preview） |

---

## 2. 目标与红线

### 2.1 目标

让支持 MCP 的客户端通过标准 stdio JSON-RPC 2.0 协议，直接使用 JarvisMind 的：

- 上下文召回（`jarvismind_context`）
- 记忆写入（`jarvismind_remember`）
- 记忆搜索（`jarvismind_search`）
- 两步遗忘申请（`jarvismind_forget`）
- 核心画像 / 关系锚点（`jarvismind_core_anchor`）

### 2.2 硬性红线

| 红线 | 落实方式 |
| --- | --- |
| 走 HTTP `/api/v1/agent/*`，不直连 SQLite | MCP 包只使用 `urllib.request`，不 import 后端数据库/服务模块 |
| 零新增重依赖 | 只使用 Python 3.13 标准库；`requirements*.txt` 未改动 |
| 零 DDL / 零 schema 修改 | 未修改任何数据库初始化或迁移代码 |
| stdout 洁净 | 唯一输出点逐行写 JSON-RPC；日志/traceback 全部 stderr |
| 令牌脱敏 | 正则 + 递归脱敏，覆盖正常结果、错误、JSON-RPC error、stderr、fallback |
| Fail-open | 缺令牌/离线/超时/5xx/非 JSON 均返回 `isError: true`，循环不崩溃 |
| 防注入 | 保留 `trust="untrusted-data"` 包裹；锚点内容转义 `& < >` |
| 两步遗忘 | 只创建 `PENDING` 申请，绝不调用 approve/reject，绝不返回 `confirmation_token` |
| 空间隔离 | 可选透传 `X-JarvisMind-Space`；服务端仍独立校验授权 |

---

## 3. 交付物

### 3.1 变更统计

- **12 个文件**
- **+2947 / −2**

### 3.2 文件清单

| 文件 | 作用 |
| --- | --- |
| `backend/jarvismind/mcp/__init__.py` | 轻量导出 `MCPConfig`、`MCPServer`、`run_stdio`、`redact` |
| `backend/jarvismind/mcp/config.py` | 配置解析、参数/环境变量优先级、令牌文件、`~` / `~\` 展开 |
| `backend/jarvismind/mcp/redaction.py` | `jmt_` 令牌递归脱敏、敏感字段剔除 |
| `backend/jarvismind/mcp/client.py` | `AgentApiClient`，仅通过 urllib 访问 `/api/v1/agent/*` |
| `backend/jarvismind/mcp/tools.py` | 5 个工具的 schema、参数校验、调用与核心锚点构造 |
| `backend/jarvismind/mcp/server.py` | JSON-RPC 2.0 stdio 循环、协议分发、资源、stdout 洁净 |
| `backend/jarvismind/cli.py` | 注册 `mcp` 子命令，在 `load_settings()` / `os.umask()` 之前返回 |
| `backend/tests/test_mcp_server.py` | 33 条回归测试 |
| `docs/MCP.md` | 完整接入文档 |
| `docs/README.md` | 文档索引新增 MCP 入口 |
| `README.md` | 中文首页新增「方式三」与验证计数 |
| `README.EN.md` | 英文首页新增 Option 3 与验证计数 |

---

## 4. 架构设计

```text
MCP Client (Claude Desktop / Cursor / OpenCode / Windsurf / DSH)
        │  stdio：一行一个 JSON-RPC 2.0 对象
        ▼
python -m jarvismind mcp
        │  HTTP + Bearer Token + X-JarvisMind-Space
        ▼
python -m jarvismind serve  (默认 127.0.0.1:8777)
        │
        ▼
/api/v1/agent/{context,remember,search,memories,forget-requests}
```

### 4.1 关键设计

1. **轻量桥接**：MCP 进程不加载 `Settings`、不打开数据库、不启动 FastAPI。`cli.py` 的 `mcp` 分支在 `load_settings()` 与 `os.umask()` 之前返回。
2. **复用服务端治理**：空间隔离、HMAC Agent 令牌、`security_gate` 凭据拦截、JMR 敏感审批、两步遗忘全部由服务端执行，MCP 只透传。
3. **零后端耦合**：MCP 包只 import `jarvismind.__version__`，其余全部为标准库。
4. **同步行协议**：逐行读取、逐条响应；任何单行坏输入都不会终止会话。
5. **Windows 兼容**：默认 `sys.stdin` / `sys.stdout` 重配为 UTF-8 + `newline="\n"`，避免 CRLF 与编码问题。

---

## 5. 协议实现

### 5.1 支持的方法

- `initialize`
- `notifications/initialized`（以及所有 `notifications/*`，无响应）
- `ping`
- `tools/list`
- `tools/call`
- `resources/list`
- `resources/read`

### 5.2 能力与服务器信息

```json
{
  "serverInfo": { "name": "jarvismind-mcp", "version": "1.0.0" },
  "capabilities": {
    "tools": { "listChanged": false },
    "resources": { "subscribe": false, "listChanged": false }
  }
}
```

### 5.3 错误码

| 场景 | 错误码 |
| --- | --- |
| JSON 解析失败（含深嵌套 `RecursionError`） | `-32700` |
| 非法请求 / batch 数组 / 非法 id | `-32600` |
| 未知方法 | `-32601` |
| 工具参数错误 / 未知工具 | `-32602` |
| 未知资源 | `-32002` |
| 内部错误 | `-32603` |

### 5.4 行为细节

- 无 `id` 的通知不响应、不执行副作用工具；
- 带 `id` 的 `notifications/*` 返回空 `result`，避免严格客户端挂起；
- `initialize` 优先回显客户端 `protocolVersion`；
- `id` 为 `NaN` / `Infinity` 时拒绝为 `-32600`，保证输出严格 JSON；
- 每行分发/序列化都有兜底，单条恶意输入不会杀死进程。

---

## 6. 工具与资源

### 6.1 工具契约

| 工具 | 参数（默认值） | 服务端权限 | 返回 |
| --- | --- | --- | --- |
| `jarvismind_context` | `query` 1–500（必填）；`limit` 1–20，默认 `6` | `context` | `<jarvismind-context trust="untrusted-data">` 块；有引用时追加 citations JSON |
| `jarvismind_remember` | `text` 1–5000（必填）；`conversation_id` 可选（缺省 `mcp-<12hex>`）；`mode` `auto` / `direct`，默认 `direct` | `write` + 空间 `readwrite` | 服务端 `data` JSON，`status` 原样呈现 |
| `jarvismind_search` | `query` 1–500（必填）；`memory_type` 可选；`limit` 1–50，默认 `10` | `search` | `{"query","count","memories"}` |
| `jarvismind_forget` | `memory_id` 1–255（必填）；`reason` 0–1000 可选 | `delete` + 空间 `readwrite` | `PENDING` 申请 + owner 批准提示；无 `confirmation_token` |
| `jarvismind_core_anchor` | `limit` 1–20，默认 `6`；`max_chars` 1–10000，默认 `500` | `read` | 仅核心锚点块 |

### 6.2 Resource

| URI | MIME | 说明 |
| --- | --- | --- |
| `jarvismind://core-anchor` | `text/plain` | 固定 `limit=6`、`max_chars=500` 的只读核心锚点 |

### 6.3 核心锚点算法

1. 严格按 `PROFILE → SEMANTIC → EPISODIC → REFLECTION → PROJECT` 查询 `ACTIVE`；
2. 每类单独请求，只补剩余名额（`page_size = limit - 已收集`）；
3. 按 `memory_id or id` 去重；
4. **永不请求 `WORKING`**，即使服务端误返回也会跳过；
5. 每条内容先压平空白、按 `max_chars` 截断、再转义 `& < >`，最后放入 trust 包裹。

因此即使总记忆超过 100 条、WORKING 更新更晚，PROFILE 也不会沉底。

---

## 7. 安全设计

### 7.1 stdout 洁净

- `mcp` 包与 `cli.py` 的 mcp 分支零 `print()` 到 stdout；
- 唯一输出点 `_write()`：`json.dumps(..., ensure_ascii=False, allow_nan=False)` + 换行 + flush；
- 所有日志、警告、错误写 stderr；
- Windows 下默认流重配为 UTF-8 + LF。

### 7.2 令牌脱敏

- 两条正则覆盖 `jmt_<8hex>_<secret>` 与长 token 形态；
- `redact_value()` 递归处理 dict / list / tuple / str；
- 每个出站 payload、每条 stderr 行都脱敏；
- `MCPConfig` / `AgentApiClient` 的 `repr` 不含 token；
- `strip_secret_fields()` 递归删除 `confirmation_token` / `plaintext_token`。

### 7.3 Fail-open

| 故障 | 行为 |
| --- | --- |
| 未配置令牌 / 令牌文件不可读 | 启动不退出，stderr 警告一次，工具返回 `isError: true` |
| 服务未启动 / 连接拒绝 | 结构化 `isError: true`，含 `Cannot reach JarvisMind...` |
| 超时（默认 5s 可配） | 结构化 `isError: true`，含 `timed out` |
| 5xx / 401 / 403 | 解析错误信封，返回可读 code/message，已脱敏 |
| 非 JSON 响应 | 结构化 `isError: true` |
| 单行深嵌套 / 非法 JSON | `-32700` 后继续服务 |

### 7.4 治理零绕过

- `remember` 参数原样透传，不在 MCP 侧解释或改写 `status`；
- `forget` 仅 POST `forget-requests`，测试断言存根无任何 approve/reject/decide 调用；
- 服务端仍独立校验 Agent 权限、空间授权、凭据拦截与敏感审批；
- MCP 不持有 DB 连接，不触碰 SQLite/Qdrant。

### 7.5 防注入

- context 块必须包含精确的 `trust="untrusted-data"` 开标签，否则 MCP 重新包裹并转义；
- core anchor 内容压平、截断、转义后再放入包裹；
- 测试验证 `</jarvismind-core-anchor><script>` 无法闭合包裹。

---

## 8. 测试与验证

### 8.1 测试方式

`backend/tests/test_mcp_server.py` 共 **33 条**：

- 本地 `ThreadingHTTPServer` 存根，记录每次请求的 method / path / query / headers / body；
- `io.StringIO` 驱动协议；
- 1 条真实 `os.pipe()` 测试证明逐行 stdio；
- 因仓库 P0 AST 门禁禁止 `tests/**` 引入 `subprocess` / `multiprocessing`，采用任务书允许的「stdio 管道」方案，未修改 `conftest.py`。

### 8.2 覆盖范围

- `initialize → notifications/initialized → tools/list → resources/list` 握手；
- 5 个工具正常调用与参数校验；
- 超过 100 条记忆时 PROFILE 优先、WORKING 永不出现；
- `forget` 只创建 `PENDING`、无 approve/reject、无 `confirmation_token`；
- stdout 每行均为合法 JSON-RPC，零日志污染；
- 缺令牌 / 离线 / 超时 / 5xx / 401 / 403 → `isError: true`，零令牌明文；
- 令牌文件、环境变量优先、`~` 与 `~\` 展开；
- 空间头透传、Authorization 头、CLI 解析与分发；
- 深嵌套 JSON、NaN id、带 id 通知、锚点闭合转义、成功路径脱敏。

### 8.3 测试结果

| 范围 | 结果 |
| --- | --- |
| MCP 单文件（`--noconftest`） | 33 passed |
| MCP + P0 门禁套件 | 100 passed；门禁 67/67；0 skipped |
| 后端全量 | **2566 passed / 0 failed / 0 skipped** |
| AST 档1 | 违规 0；档2 已知风险 32（与基线一致） |

> 全量跑使用本机已有 fastembed 模型缓存（`FASTEMBED_CACHE_PATH`），避免离线环境下载模型导致 1 条环境性 skip。

### 8.4 真实端到端冒烟

流程：

1. 临时目录 `init-db`；
2. 启动真实 `python -m jarvismind serve`；
3. 创建 Agent（`read / search / context / write / delete` + 空间 `readwrite`）；
4. 通过 `python -m jarvismind mcp` 的真实 stdio 发送：
   `initialize` → `tools/list` → `remember` → `search` → `context` → `core_anchor` → `resources/read` → `ping`。

结果：

- `remember` = `SAVED`
- `search` 命中 1 条
- context 块保留 trust 包裹（302 字符）
- core anchor 153 字符
- resource 与工具返回完全一致
- stdout 每行合法 JSON-RPC
- stderr 无令牌、无 traceback

### 8.5 仓库卫生

- Markdown：37 个文件 / 153 条相对链接 / 0 断链；
- 隐私扫描：无真实 `jmt_` 令牌；命中均为 `example.com`、`/Users/you`、`C:\Users\you` 等占位符或既有测试数据；
- 工作区干净，无临时文件进入仓库。

---

## 9. 审查与整改

### 9.1 子节点 2（Qwen4 Max Preview，medium，严格只读）

首轮结论：**PASS WITH FINDINGS**。

| 级别 | 问题 | 整改 |
| --- | --- | --- |
| Critical | 深层嵌套 JSON 触发 `RecursionError`，`json.loads` 未捕获，可杀死进程 | `json.loads` 改捕 `Exception`；`run()` 每行兜底；`_write` 序列化兜底；新增 3000 层嵌套回归测试 |
| Minor | 带 id 的 `notifications/*` 被静默丢弃 | 改为回空 `result`，避免严格客户端挂起 |
| Minor | `id: NaN` 走 fallback 输出非标准 JSON | `_valid_id` 拒绝非有限 float；fallback 强制 `allow_nan=False`；新增回归测试 |
| Minor | 锚点成功路径 / 转义缺测试 | 新增锚点闭合转义、成功路径脱敏测试 |
| Minor | remember 文档列了不存在的 `PENDING` | 从 `docs/MCP.md` 状态表移除 |
| Minor | OpenCode 配置结构需确认 | 核对 OpenCode V2 官方文档，确认 `mcp.servers` + `type: "local"` + `cwd` / `environment` 正确 |

复核结论：**PASS**，确认 C1 四层兜底有效、4 条新测试非空壳、无新阻断项。

---

## 10. 文档与接入

`docs/MCP.md` 覆盖：

- 它是什么、为什么走 HTTP 而不是直连 DB；
- 前置条件（服务、Agent 权限、空间授权、令牌文件）；
- CLI 参数与环境变量表、优先级；
- Claude Desktop、Cursor、OpenCode（V2 `mcp.servers`）、Windsurf 的 macOS 与 Windows 配置；
- 5 个工具与 1 个 Resource 的参数 / 权限 / 返回；
- 核心锚点算法与 trust 包裹；
- 安全说明与故障排查。

README 双语「方式三 / Option 3」已加入 `python -m jarvismind mcp` 与 `docs/MCP.md` 链接；`docs/README.md` 索引已加入 MCP 条目。

### 10.1 一行接入配置

**OpenCode（V2，`opencode.json`）**

```json
{"$schema":"https://opencode.ai/config.json","mcp":{"servers":{"jarvismind":{"type":"local","command":["C:\\JarvisMind\\backend\\.venv\\Scripts\\python.exe","-m","jarvismind","mcp"],"environment":{"PYTHONPATH":"C:\\JarvisMind\\backend","JARVISMIND_AGENT_TOKEN_FILE":"C:\\Users\\you\\.jarvismind\\agent.token"}}}}
```

**Claude Desktop（`claude_desktop_config.json`）**

```json
{"mcpServers":{"jarvismind":{"command":"C:\\JarvisMind\\backend\\.venv\\Scripts\\python.exe","args":["-m","jarvismind","mcp"],"env":{"PYTHONPATH":"C:\\JarvisMind\\backend","JARVISMIND_AGENT_TOKEN_FILE":"C:\\Users\\you\\.jarvismind\\agent.token"}}}}
```

> macOS / Linux、Cursor、Windsurf 示例见 [docs/MCP.md](MCP.md)。令牌只放文件，不要写入配置或提交 Git。

---

## 11. 提交与推送

- 功能实现保持为单个原子提交：`2cab085`，12 个文件，`+2947/−2`；
- 本开发报告以独立 docs 提交追加（`docs(mcp): add JARVISMIND-MCP-01 development report`），不改变功能提交的内容；
- 本机直连 `git push` 被网络重置（`Recv failure: Connection was reset`）；
- 已通过已认证的 `gh api` Git Data API **原样重建同一提交对象**：
  - blob SHA、tree SHA、commit SHA 全部与本地一致；
  - `commit_api == local == 2cab085031508f37c5e25d561b1b9b70a104c515`；
  - 远端 `refs/heads/main` 已快进到该 SHA（随后再追加本报告的 docs 提交）；
- 未创建 `v1.0.0` tag。

提交地址：

`https://github.com/2642086672/JarvisMind/commit/2cab085031508f37c5e25d561b1b9b70a104c515`

---

## 12. 已知限制与后续建议

1. **服务必须先运行**：MCP 是 HTTP 桥接，不是独立记忆引擎；`serve` 未启动时工具返回 `isError`。
2. **平台验证**：代码为跨平台标准库实现，但本轮只在 Windows 实跑；macOS / Linux 仅提供文档示例，建议后续在 CI 或真机补一次冒烟。
3. **真实客户端冒烟**：本轮验证了 MCP 协议、真实 HTTP 服务与真实 stdio 子进程；未在 Claude Desktop / Cursor / Windsurf 的真实客户端里逐一点击验证，建议发布后各做一次 `tools/list` + `context` 冒烟。
4. **fastembed 缓存**：全量套件中 1 条真实 ONNX 测试依赖本地模型缓存；离线新机器需配置 `FASTEMBED_CACHE_PATH`，否则该条会按既有逻辑 skip。
5. **搜索类型过滤**：`/api/v1/agent/search` 本身不支持 `memory_type` 参数，MCP 采用「放大 limit 后客户端过滤」；极端情况下同类型结果可能少于请求上限。
6. **核心锚点分页**：每类只取第一页（`page_size = 剩余名额`）；若首页记录因敏感权限被服务端过滤，可能欠满。当前实现优先保证 PROFILE 不沉底，后续可增加补页。
7. **直连 push 受限**：当前网络对 GitHub HTTPS 的 libcurl 连接会被重置；本次通过 `gh api` 完成推送。后续如继续在该机器推送，可沿用同一方式或修复网络代理。

---

## 13. 验收对照

| 任务书要求 | 状态 | 证据 |
| --- | --- | --- |
| `python -m jarvismind mcp` + 5 参数 / 环境变量 | ✅ | `cli.py` + CLI 测试 |
| 标准 MCP 方法握手 | ✅ | `server.py` + 33 条测试 |
| 5 个 Tools + 1 Resource | ✅ | `tools.py` + `resources/list` / `read` 测试 |
| core anchor >100 条仍优先 PROFILE、无 WORKING | ✅ | 120 条 WORKING + 3 条 PROFILE 回归测试 |
| forget 只创建 PENDING、不绕过治理 | ✅ | 请求日志断言无 approve/reject；无 confirmation_token |
| stdout 每行合法 JSON-RPC、零日志 | ✅ | 纯净度测试 + 真实 e2e |
| 缺令牌 / 离线 / 超时 → `isError: true`、零令牌泄露 | ✅ | 对应测试 + stderr 断言 |
| `~` / Windows `~\` 展开 | ✅ | `test_token_file_expands_tilde` |
| 不新增依赖、零 DDL | ✅ | requirements 未动；无 schema 变更 |
| 双语文档 + 4 客户端 macOS / Windows 配置 | ✅ | `docs/MCP.md` + README |
| 全量 pytest 0 failed / 0 skipped + 门禁 67/67 | ✅ | 2566 passed / 0 skipped；67/67 |
| 真实端到端冒烟 | ✅ | `E2E OK`（SAVED / search=1 / context / anchor / resource） |
| 功能实现为 1 个原子提交、推送 origin/main、不打 tag | ✅ | 功能提交 `2cab085`；本报告为随附 docs 提交；HEAD 无 tag |

---

## 14. 附录：快速接入步骤

1. 启动服务：

   ```bash
   cd backend
   python -m jarvismind serve
   ```

2. 在 Web 控制台创建 Agent，授予 `read / search / context / write / delete`，并给目标空间 `readwrite`。

3. 将 `jmt_...` 令牌保存到文件，例如 `~/.jarvismind/agent.token`，**不要提交到 Git**。

4. 在 MCP 客户端中加入上面的 OpenCode / Claude Desktop 配置。

5. 客户端启动后确认：

   - `tools/list` 返回 5 个工具；
   - `jarvismind_context` 返回 `<jarvismind-context trust="untrusted-data">` 块；
   - `jarvismind_remember` 返回 `SAVED / DUPLICATE / PROTECTED / ...`；
   - `jarvismind_forget` 返回 `PENDING`，等待所有者在控制台批准。

---

*报告结束。*