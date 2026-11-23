# JarvisMind 1.0

**简体中文** | [English](README.EN.md)

> 我要给她完整的一生。
> *I will give her a whole life.* —— 出自《流浪地球》（*The Wandering Earth*）

**JarvisMind** 是一个开放、可自托管的 **AI 系统统一记忆基础设施**（MIT 协议）。它让任意 AI 助手——无论是 **DeepSeek Harness（DSH）**、Claude、ChatGPT、本地 Agent 还是企业系统——在同一个地方记住、召回、治理和真正遗忘用户告诉它的一切，并提供一个可视化 Web 控制台，让所有者随时查看、修正和掌控全部记忆。

## 📣 反馈与交流

公测期间，你的每一条反馈都很重要：

* 🐛 **反馈 Bug** · 💡 **提功能建议** · 🧪 **公测体验吐槽**：[点这里选择反馈类型](https://github.com/2642086672/JarvisMind/issues/new/choose)（30 秒填完，也允许直接提空白 Issue）
* 安装卡住、控制台看不懂、陪伴模式“没那味儿”——都欢迎直接说，不用写得很正式。
* ⚠️ 反馈时请勿粘贴真实 API Token 或个人密钥；安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

## 为什么做 JarvisMind？

* **告别黑盒失忆**：不同 AI 工具共用同一套自托管记忆，不再换个客户端就从零开始。
* **防上下文压缩“失魂”**：原生上下文一满或重开新会话时，常驻 `<jarvismind-core-anchor>` 核心关系锚点会每轮重建称呼、性格默契与长期约定，避免伙伴型 AI 突然退化成陌生客服。
* **安全、可审查、可真遗忘**：API Key 与密码在入库前即被拦截；敏感个人事实需所有者授权；遗忘走两步审批，执行后同步从数据库、向量库与图谱视图中彻底消失。

## 核心能力

* **权威记忆引擎（JME）**：每个记忆空间对应独立的 SQLite 数据库，物理隔离，绝不维护会漂移的多副本。
* **写前治理闸门（JMR）**：自动拦截凭据与密钥；敏感个人事实进入待审授权；Agent 不能替自己审批，也不能越权修改记忆。
* **派生知识图谱（JKG，开箱即用）**：从记忆审计流自动派生人物、项目、事件与关系视图；全新 `init-db` 与新建空间默认建好图谱表，首次启动即可在控制台查看。
* **上下文召回引擎（MCE）**：关键词 + 向量混合检索，按预算拼装上下文并显式标记为不可信数据（`trust="untrusted-data"`），防止提示词注入。
* **Web 统一控制台**：浏览器一站式管理记忆、知识图谱、治理审批、多记忆空间、Agent 令牌与权限、运维、API 与备份。
* **API v1、双语言 SDK 与原生 DSH 插件**：提供版本化 `/api/v1/agent/*` 接口、零运行时依赖的 [Python SDK](sdk/python/README.md) 与 [TypeScript SDK](sdk/typescript/README.md)，以及面向 **DeepSeek Harness Desktop** 的原生 Cordis 插件（[plugins/dsh-jarvismind](plugins/dsh-jarvismind/README.md)）。

## 近期核心更新

* **由模型自主判断“什么值得记”（`rememberMode: agent`）**：不再每轮把“嗯 / 好的 / 谢谢”盲目写入，而是由当前会话 AI 提炼真正有长期价值的信息调用 `jarvismind_remember`，后端通过 `mode: direct` 在完整保留凭据拦截、敏感治理与去重的前提下直接落库。
* **回合末兜底，因为模型真的会忘**：某一轮结束时若模型根本没调用工具，就用 `mode: auto` 补交一次有实质内容的用户消息，仍由服务端判定是否值得存。模型本轮已记录过则不重复提交，寒暄也不会触发。`fallbackCaptureOnTurnEnd: false` 可关闭。
* **批量写入**：DSH 插件与 MCP 的 `jarvismind_remember` 都接受 `items`（单次最多 20 条），粘贴一份导出的记忆可以一次入库。每条独立走治理接口，结果汇总 saved / duplicate / protected / denied / failed。两种记忆风格也都要求模型**先把粘贴材料里的事实存进去，再写总结**。
* **首轮不再超时**：插件 `timeoutMs` 默认值改为 `6000`；`serve` 在监听就绪后用后台线程预热嵌入模型（`JARVISMIND_WARMUP_EMBEDDING=0` 可关闭）。冷启动的模型加载以前正好落在第一个请求里。
* **数据目录与代码解耦**：安装文档改为推荐固定 `JARVISMIND_DATA_DIR`（如 `~/.jarvismind/data`），升级或重新克隆仓库时不会再出现“记忆全没了”的错觉。
* **`companion` 陪伴记忆模式**：除常规工作效率风格（`assistant`）外，开启 `companion` 后可引导 AI 留意共同回忆、内部梗、重要日期、情绪时刻与相处约定，且**静默记录、绝不播报“已保存”**，保持自然沉浸感。
* **核心灵魂锚点突破 100 条记忆限制**：`<jarvismind-core-anchor>` 优先按 `PROFILE` 类型单独查询，再补充其他类型（最多读取最新 100 条 `PROFILE`），早期建立的称呼与核心约定不会被后续新记忆挤掉。
* **知识图谱开箱即用**：`init-db` 与新建空间在建库事务内自动建表并开启 `jkg.enabled`，首启即 `ready`。
* **Windows 双平台验证与依赖拆分**：修复 Windows 控制台静态资源与 `~\` 令牌路径问题；将测试依赖独立拆入 `backend/requirements-dev.txt`。

## 系统架构

```text
   所有者（浏览器控制台）                    智能体与应用（DSH、DeepSeek、Claude、自定义 …）
   所有者凭据                                智能体令牌（jmt_...），按空间最小授权
        |                                                       |
        v                                                       v
  /api/v1/console, /api/v1/memories, ...                 /api/v1/agent/*
        |                                                       |
        +--------------------------+----------------------------+
                                   v
                JarvisMind API（FastAPI，单进程）
                +----------------------------------------------------------+
                |  MCE  上下文与召回   --读取-->  JME  单一权威记忆          |
                |  JMR  治理闸门      --守护-->  JME  写入 / 审计           |
                |  JKG  知识图谱(派生) <--审计流-- JME  审计日志            |
                +----------------------------------------------------------+
                                   |
        control.db       : 空间、智能体、授权、HMAC 令牌摘要、访问日志（仅元数据）
        jarvismind.db    : 默认记忆空间（JME + JMR + JKG 表，schema v6）
        spaces/<id>/     : 每个额外空间的物理隔离存储（独立 SQLite + 向量目录）
```

详细设计原则、写入与两步遗忘路径、隔离模型见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 3 分钟快速开始

环境要求：**Python 3.13**（发布实测版本）；构建 Web 控制台需要 **Node.js 20+**。

### 1. 克隆并构建 Web 控制台

```bash
git clone https://github.com/2642086672/JarvisMind.git
cd JarvisMind
(cd console && npm ci && npm run build)
```

### 2. 初始化并启动服务

> **长期使用请先把数据目录移出代码仓库。** 默认数据库在 `backend/data/`，升级或重新克隆仓库时很容易整目录换掉，让人误以为“记忆全丢了”。设置一个固定目录，记忆库与令牌就不随代码走：
>
> ```bash
> export JARVISMIND_DATA_DIR="$HOME/.jarvismind/data"   # 写进 ~/.zshrc 长期生效
> ```
>
> Windows PowerShell：`[Environment]::SetEnvironmentVariable('JARVISMIND_DATA_DIR', "$HOME\.jarvismind\data", 'User')`

**macOS / Linux：**

```bash
export JARVISMIND_DATA_DIR="$HOME/.jarvismind/data"
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m jarvismind init-db
JARVISMIND_CONSOLE_DIR="$(cd ../console && pwd)/dist" python -m jarvismind serve
```

**Windows（PowerShell）：**

```powershell
$env:JARVISMIND_DATA_DIR = "$HOME\.jarvismind\data"
cd backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m jarvismind init-db
$env:JARVISMIND_CONSOLE_DIR = (Resolve-Path ..\console\dist).Path
python -m jarvismind serve
```

### 3. 验证与登录控制台

```bash
curl -s http://127.0.0.1:8777/health
```

首次启动会在 `<数据目录>/.jarvismind_token` 自动生成所有者令牌（权限 `0600`，也可通过 `JARVISMIND_API_TOKEN` 覆盖）。未设置 `JARVISMIND_DATA_DIR` 时数据目录为 `backend/data/`。浏览器打开 `http://127.0.0.1:8777/console/` 登录即可。

如需运行后端测试套件，安装开发依赖后再执行 `pytest`：

```bash
pip install -r requirements-dev.txt
python -m pytest
```

详细分平台指南：[安装指南](docs/INSTALLATION.md) · [配置说明](docs/CONFIGURATION.md) · [Docker](docs/DOCKER.md) · [macOS](docs/deployment/MACOS.md) · [Windows](docs/deployment/WINDOWS.md) · [Linux](docs/deployment/LINUX.md)

## 接入 DeepSeek Harness（DSH）与其他智能体

### 方式一：DSH Desktop 原生插件（推荐）

1. 在控制台 **Agent 管理** 中新建 `DSH` 智能体、授予空间 `readwrite` 权限并颁发 `jmt_...` 令牌，保存至 `~/.jarvismind/dsh-agent.token`（也可运行 `examples/dsh-integration/python/setup_agent.py` 自动创建）。
2. 在 DSH Desktop 中打开 **Plugins → Add plugin**，填入本地 `plugins/dsh-jarvismind` 目录路径并启用 **JarvisMind Memory**；可在插件配置中一键切换 `rememberMode`（默认 `agent`）与 `memoryStyle`（`assistant` / `companion`）。

完整步骤见 [plugins/dsh-jarvismind/README.md](plugins/dsh-jarvismind/README.md) 与 [docs/tutorials/INTEGRATE-WITH-DSH.md](docs/tutorials/INTEGRATE-WITH-DSH.md)。

### 方式二：任意应用通过 API / SDK 接入

1. 回复前调用 `POST /api/v1/agent/context`，将返回的 `<jarvismind-context>` 作为只读参考数据放入 system prompt；
2. 对话中由模型或后置钩子调用 `POST /api/v1/agent/remember` 提交记忆；
3. 遗忘走两步流程，由所有者在控制台最终批准。

详见 [docs/AGENT-INTEGRATION.md](docs/AGENT-INTEGRATION.md)、[Python SDK](sdk/python/README.md) 与 [TypeScript SDK](sdk/typescript/README.md)。

### 方式三：MCP 原生接入

运行 `python -m jarvismind mcp` 即可把 JarvisMind 作为原生 MCP Server 接入 Claude Desktop、Cursor、OpenCode、Windsurf 等支持 MCP 的客户端。它通过 stdio 桥接本机 HTTP 服务，零新增依赖，并复用服务端鉴权、空间隔离与两步遗忘流程。完整配置（含 macOS 与 Windows 示例）见 [docs/MCP.md](docs/MCP.md)。

## 当前验证状态（诚实口径）

| 模块 / 平台 | 当前验证结果 |
| --- | --- |
| 后端全量测试（含 P0 门禁） | 2578 通过 / 0 失败 / 0 跳过；门禁 67/67 |
| Web 控制台 | TypeScript 0 错误，15 项单元测试通过，生产构建成功 |
| Python SDK / TypeScript SDK | 10 项 / 9 项测试通过 |
| DSH 原生插件 | 92 项测试（macOS 上 91 通过 + 1 项仅 Windows 运行）；构建无错；DSH Desktop `0.2.0-rc.2` 真实运行时 `SMOKE OK` |
| DSH 跨会话演示 | 16/16 检查通过（Python 与 TypeScript 均对真实服务运行） |
| Windows 实测（本机） | 已跑通后端测试与控制台构建；浏览器冒烟（登录、仪表盘、图谱 `ready`、明暗主题切换零报错）。macOS 与 Linux 尚未实测 |
| Docker 镜像、Linux 安装、独立终审 | **尚未完成**（见 [RELEASE-1.0-FINAL-REPORT.md](RELEASE-1.0-FINAL-REPORT.md)，暂未打 `v1.0.0` Tag） |

> 离线新机器上首次列出记忆时，如本地无嵌入模型缓存，可能因模型下载重试等待约 40 秒。`serve` 启动后会在后台线程预热嵌入模型，把这份开销从第一个请求上移走（`JARVISMIND_WARMUP_EMBEDDING=0` 可关闭）；配置 `FASTEMBED_CACHE_PATH` 或预先缓存模型能进一步避免。

## 文档索引与安全

* 全量文档索引：[docs/README.md](docs/README.md) · [API 参考](docs/API-REFERENCE.md) · [知识图谱](docs/KNOWLEDGE-GRAPH.md) · [路线图](docs/ROADMAP.md)
* 安全说明与审计现状：[docs/SECURITY.md](docs/SECURITY.md) · [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md)
* 参与贡献：[CONTRIBUTING.md](CONTRIBUTING.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [CHANGELOG.md](CHANGELOG.md)

## 许可证

本项目基于 [MIT License](LICENSE) 开源。
