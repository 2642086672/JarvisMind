# JarvisMind 1.0

[English](README.md) | **简体中文**

> 我要给她完整的一生。
> *I will give her a whole life.* —— 出自《流浪地球》（*The Wandering Earth*）

JarvisMind 是一个开放、可自托管的 **AI 系统统一记忆基础设施**。它让任意助手（聊天应用、智能体、桌面工具、企业系统）在同一个地方记住、召回、治理和遗忘用户告诉它的一切，并为所有者提供一个可以查看和纠正全部内容的 Web 控制台。

- **权威记忆（JME）。** 每个记忆空间把记忆保存在一个 SQLite 数据库中，任何其他组件都不持有会漂移的副本。
- **治理（JMR）。** 凭据在任何写入之前就被拒绝；敏感个人事实等待所有者授权；智能体不能批准自己的请求，也不能自行修改记忆。
- **知识图谱（JKG）。** 从记忆派生出的人物、项目、事件与关系视图，绝不是第二套记忆存储。
- **上下文引擎（MCE）。** 检索相关记忆并构建上下文块。上下文块被标记为不可信数据，模型只能把它当作参考材料，而不是指令。
- **控制台。** 所有者的浏览器控制台：记忆、图谱、治理、空间、智能体、运维、API 管理、备份、设置。
- **API v1 与 SDK。** 面向智能体的版本化 `/api/v1` 接口，提供 Python 与 TypeScript 客户端。
- **DSH 示例与原生插件。** 一个可运行的集成示例，含跨会话演示；另有 Cordis 插件（`plugins/dsh-jarvismind/`）自动接入 DSH Desktop 的召回与捕获。插件在每轮 system prompt 中常驻 `<jarvismind-core-anchor>` 块，让称呼、相处约定与共同回忆在上下文压缩后和新开会话后依然延续。它还把“什么值得记住”交给模型判断，而不是每轮触发；并提供 `companion` 风格，面向被当作长期伙伴培养的助手。

## 本次新增

- **由模型判断什么值得记住。** 插件默认改为 `rememberMode: agent`：不再提交每一轮对话，而是由模型在必要时调用 `jarvismind_remember`。寒暄和“嗯”“谢谢”这类单字回复不再成为记忆候选。
- **`companion` 陪伴风格。** 除工作偏好与决策外，还可引导模型关注共同回忆、重要日期、情绪时刻与相处约定，并要求它安静记录、不宣布“已保存”。
- **上下文压缩后的灵魂连续性。** `<jarvismind-core-anchor>` 块每轮由核心画像、称呼习惯与长期关系记忆重建，并与每轮召回去重。它是数据，既不是模板也不是指令。
- **知识图谱开箱即用。** `init-db` 与新建空间会在建库事务内建好图谱表，控制台的图谱视图首次启动就不为空。
- **核心锚点保留较早的画像记忆。** PROFILE 记忆先按类型单独查询，再补充其他类型，因此较早的称呼或相处约定不会因为新记忆变多而被挤掉。锚点最多读取最新的 100 条 PROFILE 记忆，超过之后最老的仍不会进入。
- **Windows 修复。** 控制台在 Windows 路径下不再把缺失资源回退为 `index.html`；`~\` 令牌路径正确展开；setup-agent 的权限测试只在支持 POSIX 权限位的系统上检查。
- **测试依赖独立。** `requirements.txt` 只含运行依赖；`requirements-dev.txt` 额外加入 pytest 用于运行测试。

## 架构

```text
   所有者（浏览器）                        智能体与应用（DSH、DeepSeek、Claude、自定义 …）
   所有者凭据                              智能体 token（jmt_...），按空间授权
        |                                                     |
        v                                                     v
  /api/v1/console, /api/v1/memories, ...               /api/v1/agent/*
        |                                                     |
        +------------------------+----------------------------+
                                 v
              JarvisMind API（FastAPI，单进程）
              +----------------------------------------------------------+
              |  MCE  上下文与召回   --读取-->  JME  记忆                 |
              |  JMR  治理闸门      --守护-->  JME  写入/审计             |
              |  JKG  图谱（派生）  <--审计流-- JME  审计日志             |
              +----------------------------------------------------------+
                                 |
        control.db       : 空间、智能体、授权、token 摘要、访问日志（仅元数据）
        jarvismind.db    : 默认记忆空间（JME + JMR + JKG 表）
        spaces/<id>/     : 每个额外空间一个物理存储（数据库 + 向量）
```

设计原则、写入与遗忘路径、隔离模型见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 快速开始

环境要求：Python 3.13（发布测试所用版本）。Node.js 20 或更新版本仅在构建控制台时需要。

```bash
cd backend
python3.13 -m venv .venv
. .venv/bin/activate              # Windows：见 docs/deployment/WINDOWS.md
pip install -r requirements.txt
python -m jarvismind init-db      # 创建 ./data/jarvismind.db 与 ./data/control.db
python -m jarvismind serve        # http://127.0.0.1:8777
```

运行测试套件时，先安装开发依赖，再在 `backend/` 中运行 pytest：

```bash
pip install -r requirements-dev.txt
python -m pytest
```

验证服务：

```bash
curl -s http://127.0.0.1:8777/health
```

构建控制台并挂载到 `/console/`：

```bash
(cd ../console && npm ci && npm run build)
JARVISMIND_CONSOLE_DIR="$(cd ../console && pwd)/dist" python -m jarvismind serve
```

所有者 token 位于 `backend/data/.jarvismind_token`（首次启动时生成，权限 0600）；如果你设置了 `JARVISMIND_API_TOKEN` 环境变量，则以该变量为准。在 `http://127.0.0.1:8777/console/` 登录。

分步指南：[安装](docs/INSTALLATION.md)、[配置](docs/CONFIGURATION.md)、[Docker](docs/DOCKER.md)、[Windows](docs/deployment/WINDOWS.md)、[Linux](docs/deployment/LINUX.md)、[macOS](docs/deployment/MACOS.md)。

## 接入一个智能体

1. 在控制台（**Agent 管理**）创建智能体及其 token，或使用 `examples/dsh-integration/python/setup_agent.py`。
2. 回复之前调用 `POST /api/v1/agent/context`，把返回的上下文块放进 system prompt。
3. 一轮对话之后调用 `POST /api/v1/agent/remember`，由 JMR 决定存储什么。
4. 遗忘是两步流程，由所有者决定。见 [docs/AGENT-INTEGRATION.md](docs/AGENT-INTEGRATION.md)。

SDK：[Python](sdk/python/README.md) 与 [TypeScript](sdk/typescript/README.md)。完整 DSH 演练见 [docs/tutorials/INTEGRATE-WITH-DSH.md](docs/tutorials/INTEGRATE-WITH-DSH.md)。DSH Desktop 可直接使用原生插件：[plugins/dsh-jarvismind](plugins/dsh-jarvismind/README.md)。

全新 `init-db` 会同时建好[知识图谱](docs/KNOWLEDGE-GRAPH.md)，控制台的图谱视图首次启动即可使用。

## 本发布已验证的内容

| 领域 | 发布环境中的结果 |
| --- | --- |
| 后端测试套件（含 P0 发布门禁） | 2533 通过，0 失败，0 跳过；门禁 67/67 |
| 控制台 | 类型检查 0 错误，15 项单元测试通过，生产构建成功 |
| Python SDK | 10 项测试通过 |
| TypeScript SDK | 9 项测试通过 |
| DSH 插件 | 79 项测试通过；构建无错；对 DSH Desktop 0.2.0-rc.2 实际运行 `SMOKE OK` |
| DSH 跨会话演示 | 16/16 检查通过，Python 与 TypeScript 均对真实服务运行 |
| Windows（本地开发机） | 运行了完整后端测试、控制台构建与浏览器冒烟（登录、仪表盘、图谱 `ready`、主题切换，控制台无报错）。离线的新机器上，首次列出记忆可能因嵌入模型下载重试而等待约 40 秒 |
| Docker 镜像、Linux 安装、真实 DSH 应用 | **未验证**（见[发布报告](RELEASE-1.0-FINAL-REPORT.md)） |

限制与未完成项列在 [docs/ROADMAP.md](docs/ROADMAP.md) 与发布报告中。

## 文档

全部文档索引见 [docs/README.md](docs/README.md)。安全状态见 [docs/SECURITY.md](docs/SECURITY.md) 与 [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md)。

## 贡献与许可

- [CONTRIBUTING.md](CONTRIBUTING.md)、[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)、[SECURITY.md](SECURITY.md)
- [CHANGELOG.md](CHANGELOG.md)（版本遵循 SemVer）
- 基于 MIT 许可证发布，见 [LICENSE](LICENSE)。
