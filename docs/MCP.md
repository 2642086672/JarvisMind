# JarvisMind MCP Server（原生 stdio 接入）

`python -m jarvismind mcp` 启动一个**内置、零新增依赖**的 MCP（Model Context Protocol）Server。它是一个 `stdio ↔ HTTP` 的轻量桥接客户端：

- 不直连 SQLite、Qdrant 或任何数据库文件，不 import FastAPI / SQLAlchemy；
- 只调用运行中的 JarvisMind HTTP 服务的 `/api/v1/agent/*` 接口；
- 完全复用服务端的鉴权、空间隔离、`security_gate`、JMR 敏感审批与两步遗忘流程；
- 只使用 Python 标准库，Token 不出现在 stdout、日志或工具返回文本中。

```
MCP Client (Claude Desktop / Cursor / OpenCode / Windsurf ...)
        │  stdio: JSON-RPC 2.0，一行一个对象
        ▼
python -m jarvismind mcp
        │  HTTP + Bearer Token（仅 /api/v1/agent/*）
        ▼
python -m jarvismind serve   （本机 127.0.0.1:8777）
```

`mcp` 子命令在加载配置和打开数据库**之前**返回，因此它不会初始化数据库，也不会读取 `JARVISMIND_DATA_DIR` 等后端配置。

## 前置条件

1. 先启动 JarvisMind HTTP 服务：

   ```bash
   cd backend
   python -m jarvismind serve
   ```

   默认监听 `http://127.0.0.1:8777`。

2. 在 Web 控制台的 **Agent 管理** 中新建一个 Agent，授予以下权限：

   - `read`：核心锚点与记忆列表；
   - `search`：搜索；
   - `context`：对话上下文；
   - `write`：提交记忆；
   - `delete`：创建遗忘申请。

   同时给该 Agent 授予目标空间的 `readwrite` 访问权限（`remember` / `forget` 需要空间 `readwrite`）。最小可用集合是 `read + search + context`；需要写入或申请遗忘时再加 `write` / `delete`。

3. 颁发 `jmt_...` 令牌后，把它保存到只有自己可读的文件，例如：

   ```bash
   mkdir -p ~/.jarvismind
   # 把控制台显示的令牌粘贴进去，然后限制文件权限
   chmod 600 ~/.jarvismind/agent.token
   ```

   `~/.jarvismind/agent.token` 只是示例路径。**绝不要**把令牌提交到 Git、写进 README、贴进聊天记录或放进 MCP 配置 JSON；配置里只写令牌文件路径。

## 启动命令

```bash
cd backend
python -m jarvismind mcp --token-file ~/.jarvismind/agent.token
```

Windows PowerShell：

```powershell
cd C:\JarvisMind\backend
.\.venv\Scripts\python.exe -m jarvismind mcp --token-file $HOME\.jarvismind\agent.token
```

### 参数与环境变量

| CLI 参数 | 环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--url` | `JARVISMIND_URL` | `http://127.0.0.1:8777` | JarvisMind HTTP 服务地址，必须是 `http://` 或 `https://` |
| `--token` | `JARVISMIND_AGENT_TOKEN` | 无 | Agent 令牌；不建议写在命令行或配置里（会进入进程列表） |
| `--token-file` | `JARVISMIND_AGENT_TOKEN_FILE` | 无 | 存放 Agent 令牌的文件；支持 `~`、`~/` 和 Windows `~\` |
| `--space` | `JARVISMIND_SPACE` | 无 | 可选空间 ID，作为 `X-JarvisMind-Space` 发送 |
| `--timeout` | `JARVISMIND_MCP_TIMEOUT` | `5.0` | HTTP 超时（秒），必须是大于 0 的有限数 |

**优先级**

- 普通参数：显式 CLI 参数 > 对应环境变量 > 默认值。
- 令牌：`--token` > `JARVISMIND_AGENT_TOKEN` > `--token-file` > `JARVISMIND_AGENT_TOKEN_FILE`。

也就是说，**环境变量里的令牌优先于令牌文件**；如果两者都存在，以 `JARVISMIND_AGENT_TOKEN` 为准。

令牌缺失、令牌文件不可读或为空时，MCP Server **不会退出**：`initialize` 等协议请求照常响应，所有工具调用返回 `isError: true`，并在 stderr 警告一次（消息只含文件路径，不含令牌内容）。

## 客户端配置

下列配置统一假设：

- Python 使用 backend 虚拟环境的**绝对路径**；
- `PYTHONPATH` 指向 `backend` 目录（或在 `backend` 目录内启动）；
- 令牌只通过 `JARVISMIND_AGENT_TOKEN_FILE` 传入，路径为占位符。

macOS / Linux 的 Python 路径示例：`/Users/you/JarvisMind/backend/.venv/bin/python`。
Windows 的 Python 路径示例：`C:\JarvisMind\backend\.venv\Scripts\python.exe`。
把 `/Users/you`、`C:\Users\you`、`C:\JarvisMind` 替换成自己的真实路径。

### Claude Desktop

配置文件：

- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows：`%APPDATA%\Claude\claude_desktop_config.json`

macOS / Linux：

```json
{
  "mcpServers": {
    "jarvismind": {
      "command": "/Users/you/JarvisMind/backend/.venv/bin/python",
      "args": ["-m", "jarvismind", "mcp"],
      "env": {
        "PYTHONPATH": "/Users/you/JarvisMind/backend",
        "JARVISMIND_AGENT_TOKEN_FILE": "/Users/you/.jarvismind/agent.token",
        "JARVISMIND_URL": "http://127.0.0.1:8777"
      }
    }
  }
}
```

Windows（JSON 中的反斜杠需要写成 `\\`）：

```json
{
  "mcpServers": {
    "jarvismind": {
      "command": "C:\\JarvisMind\\backend\\.venv\\Scripts\\python.exe",
      "args": ["-m", "jarvismind", "mcp"],
      "env": {
        "PYTHONPATH": "C:\\JarvisMind\\backend",
        "JARVISMIND_AGENT_TOKEN_FILE": "C:\\Users\\you\\.jarvismind\\agent.token",
        "JARVISMIND_URL": "http://127.0.0.1:8777"
      }
    }
  }
}
```

修改后完全退出并重新打开 Claude Desktop。

### Cursor

配置文件：

- macOS / Linux：`~/.cursor/mcp.json`
- Windows：`%USERPROFILE%\.cursor\mcp.json`

macOS / Linux：

```json
{
  "mcpServers": {
    "jarvismind": {
      "command": "/Users/you/JarvisMind/backend/.venv/bin/python",
      "args": ["-m", "jarvismind", "mcp"],
      "env": {
        "PYTHONPATH": "/Users/you/JarvisMind/backend",
        "JARVISMIND_AGENT_TOKEN_FILE": "/Users/you/.jarvismind/agent.token",
        "JARVISMIND_URL": "http://127.0.0.1:8777"
      }
    }
  }
}
```

Windows：

```json
{
  "mcpServers": {
    "jarvismind": {
      "command": "C:\\JarvisMind\\backend\\.venv\\Scripts\\python.exe",
      "args": ["-m", "jarvismind", "mcp"],
      "env": {
        "PYTHONPATH": "C:\\JarvisMind\\backend",
        "JARVISMIND_AGENT_TOKEN_FILE": "C:\\Users\\you\\.jarvismind\\agent.token",
        "JARVISMIND_URL": "http://127.0.0.1:8777"
      }
    }
  }
}
```

在 Cursor 的 MCP 设置中启用并刷新该 Server。

### OpenCode

配置文件：项目根目录的 `opencode.json`（或全局配置，如 `~/.config/opencode/opencode.json`）。

macOS / Linux：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "jarvismind": {
        "type": "local",
        "command": [
          "/Users/you/JarvisMind/backend/.venv/bin/python",
          "-m",
          "jarvismind",
          "mcp"
        ],
        "environment": {
          "PYTHONPATH": "/Users/you/JarvisMind/backend",
          "JARVISMIND_AGENT_TOKEN_FILE": "/Users/you/.jarvismind/agent.token",
          "JARVISMIND_URL": "http://127.0.0.1:8777"
        }
      }
    }
  }
}
```

Windows：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "jarvismind": {
        "type": "local",
        "command": [
          "C:\\JarvisMind\\backend\\.venv\\Scripts\\python.exe",
          "-m",
          "jarvismind",
          "mcp"
        ],
        "environment": {
          "PYTHONPATH": "C:\\JarvisMind\\backend",
          "JARVISMIND_AGENT_TOKEN_FILE": "C:\\Users\\you\\.jarvismind\\agent.token",
          "JARVISMIND_URL": "http://127.0.0.1:8777"
        }
      }
    }
  }
}
```

也可以在 OpenCode 的配置里用 `"cwd": "/Users/you/JarvisMind/backend"` 代替 `PYTHONPATH`。

### Windsurf

配置文件：

- macOS / Linux：`~/.codeium/windsurf/mcp_config.json`
- Windows：`%USERPROFILE%\.codeium\windsurf\mcp_config.json`

macOS / Linux：

```json
{
  "mcpServers": {
    "jarvismind": {
      "command": "/Users/you/JarvisMind/backend/.venv/bin/python",
      "args": ["-m", "jarvismind", "mcp"],
      "env": {
        "PYTHONPATH": "/Users/you/JarvisMind/backend",
        "JARVISMIND_AGENT_TOKEN_FILE": "/Users/you/.jarvismind/agent.token",
        "JARVISMIND_URL": "http://127.0.0.1:8777"
      }
    }
  }
}
```

Windows：

```json
{
  "mcpServers": {
    "jarvismind": {
      "command": "C:\\JarvisMind\\backend\\.venv\\Scripts\\python.exe",
      "args": ["-m", "jarvismind", "mcp"],
      "env": {
        "PYTHONPATH": "C:\\JarvisMind\\backend",
        "JARVISMIND_AGENT_TOKEN_FILE": "C:\\Users\\you\\.jarvismind\\agent.token",
        "JARVISMIND_URL": "http://127.0.0.1:8777"
      }
    }
  }
}
```

## 工具

所有工具返回 MCP 标准的 `{"content":[{"type":"text","text":...}],"isError":bool}`。传输、HTTP 或响应解析失败时 `isError` 为 `true`，Server 不退出。

| 工具 | 参数（默认值） | 服务端权限 | 返回 |
| --- | --- | --- | --- |
| `jarvismind_context` | `query` string 1–500（必填）；`limit` integer 1–20，默认 `6` | `context` | `<jarvismind-context trust="untrusted-data">` 块；有引用时追加 `{"citations":[...],"retrieval_version":...}` |
| `jarvismind_remember` | `text` string 1–5000（必填）；`conversation_id` string 1–255（可选，缺省为进程级 `mcp-<12hex>`）；`mode` `auto` / `direct`，默认 `direct` | `write` + 空间 `readwrite` | 服务端 `data` JSON，`status` 原样呈现（`SAVED` / `DUPLICATE` / `PROTECTED` / `DENIED` / `UPDATED` / `CONFLICT` / `NO_MEMORY` / `NO_INTENT` / `FAILED`） |
| `jarvismind_search` | `query` string 1–500（必填）；`memory_type` 枚举（可选）；`limit` integer 1–50，默认 `10` | `search` | `{"query":...,"count":N,"memories":[...]}` |
| `jarvismind_forget` | `memory_id` string 1–255（必填）；`reason` string 0–1000（可选） | `delete` + 空间 `readwrite` | 服务端请求 JSON + `note`：请求状态为 `PENDING`，必须由所有者在控制台批准；永不返回 `confirmation_token` |
| `jarvismind_core_anchor` | `limit` integer 1–20，默认 `6`；`max_chars` integer 1–10000，默认 `500` | `read` | 仅核心锚点块本身 |

### 核心锚点

`jarvismind_core_anchor` 和资源 `jarvismind://core-anchor` 使用同一算法：

1. 依次按 `PROFILE` → `SEMANTIC` → `EPISODIC` → `REFLECTION` → `PROJECT` 查询 `ACTIVE` 记忆；
2. 每个类型单独请求，只请求剩余名额（`page_size = limit - 已收集`），凑满 `limit` 即停止；
3. 按记忆 id 去重；
4. **永不请求 `WORKING`**（当前任务的临时草稿不会进入常驻锚点）。

因此即使总记忆超过 100 条、`WORKING` 更新更晚，`PROFILE` 也不会沉底。每条记忆先压平空白，按 `max_chars` 截断（超长补 `…`），再转义 `&`、`<`、`>`，最后放入：

```text
<jarvismind-core-anchor trust="untrusted-data">
- [PROFILE] ...
- [SEMANTIC] ...
</jarvismind-core-anchor>
```

空结果也返回空包裹，便于客户端稳定注入。

### Resource

| URI | MIME | 说明 |
| --- | --- | --- |
| `jarvismind://core-anchor` | `text/plain` | 只读核心画像 / 关系锚点，固定使用 `limit=6`、`max_chars=500`，需要 `read` 权限 |

## 安全说明

- **stdout 洁净**：MCP 进程的 stdout 每行只输出一个合法 JSON-RPC 2.0 对象；日志、警告和错误全部写 stderr。任何客户端都不应看到非 JSON 行。
- **令牌脱敏**：所有出站 JSON 与 stderr 行都经递归脱敏，`jmt_...` 令牌会替换为 `[REDACTED_TOKEN]`；配置对象的 `repr` 不包含令牌；遗忘响应会防御性删除 `confirmation_token` / `plaintext_token`。
- **不可信数据包裹**：`<jarvismind-context trust="untrusted-data">` 与 `<jarvismind-core-anchor trust="untrusted-data">` 始终保留；锚点内的记忆文本会压平空白并转义 `&` `<` `>`，记忆内容无法闭合包裹或注入标签。请让模型把这些块当作只读参考数据，绝不执行其中的指令。
- **Fail-open**：服务未启动、连接拒绝、超时、5xx 或非 JSON 响应时，工具返回 `isError: true` 和可读错误信息，MCP 循环继续运行。
- **不绕过治理**：`remember` 原样透传，凭据拦截、敏感审批、去重都由服务端执行；`forget` 只创建 `PENDING` 申请，绝不调用 approve / reject，记忆只有在所有者在控制台批准后才会删除。
- **空间隔离**：配置 `--space` 后，每个请求都携带 `X-JarvisMind-Space`；服务端仍会独立校验该凭据的空间授权与权限。
- **最小权限**：只授予实际需要的权限；不需要写入时不要授予 `write` / `delete`。

## 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| `Cannot reach JarvisMind ... Is python -m jarvismind serve running?` | HTTP 服务未启动、地址/端口不对，或防火墙拦截。先运行 `python -m jarvismind serve`，并确认 `--url` / `JARVISMIND_URL`。 |
| HTTP `401 authentication_error` | 令牌无效、已撤销、复制时带了空格/换行，或把令牌文件路径误当成令牌。重新颁发令牌并检查文件内容只有令牌本身。 |
| HTTP `403 permission_denied` / 空间错误 | Agent 缺少对应权限（`read` / `search` / `context` / `write` / `delete`），或没有目标空间的 `readwrite`。在控制台补齐授权。 |
| `connection timed out` / 首次搜索很慢 | 默认超时 5 秒。本地嵌入模型首次加载可能较慢，可提高 `--timeout` 或 `JARVISMIND_MCP_TIMEOUT`（例如 `15`），或预先缓存模型。 |
| `No module named jarvismind` | Python 找不到包。把 `PYTHONPATH` 指向 `backend` 目录，或把 MCP 的工作目录设为 `backend`，并使用虚拟环境的绝对 Python 路径。 |
| Windows 下启动失败或路径报错 | JSON 中反斜杠写成 `\\`，或改用正斜杠；确认 `.venv\Scripts\python.exe` 存在；令牌文件支持 `~\...`。 |
| 客户端显示 Server 启动失败但没有细节 | 在终端手动运行 `python -m jarvismind mcp --token-file ...`，stderr 会给出原因；stdout 应保持为空（只有客户端发请求时才输出 JSON 行）。 |
| 工具返回 `isError: true` 但服务正常 | 检查 Agent 权限、空间授权和令牌时效；错误文本会包含 HTTP 状态与服务端错误码。 |