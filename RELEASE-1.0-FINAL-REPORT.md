# JarvisMind 1.0 发布报告

- 版本：1.0.0
- 仓库：`JarvisMind/`，独立 git 仓库，分支 `main`，本地 6 个提交，尚未打 `v1.0.0` 标签（待独立审计通过后再打），未配置远端
- 日期：2026-11-22（本机时间，+0800）
- 证据：`JarvisMind/.scratch/release-check/` 与 `JarvisMind/.scratch/*.log`（仅本地，已被 `.gitignore` 排除，不进入仓库）

本报告按任务书第 31 节的 11 项组织，并附 Phase 0 审计摘要（附录 A）与验证命令（附录 B）。

---

## 0. 结论

JarvisMind 1.0.0 的后端、统一 API v1、Web 控制台、Python 与 TypeScript SDK、DSH 示例和文档已经全部写完，并在本机完成了可执行的验证。核对本报告时，已把若干阶段的状态从“完成”改为“部分”；界面与任务书之间的缺口列在第 1.1 节与第 10 节第 15 项。

**但不能宣称“JarvisMind 1.0 & Console Full Development Completed”。** 按任务书第 31 节的最终验收标准，以下条件尚未满足：

1. 独立审计尚未通过。本次有两轮只读审查和一轮自查，没有独立 Audit Agent 的最终审计结论。
2. P1 项 O-01 已收窄但未完全消除。任务书第 16 节规定 P0/P1 未解决不得宣称完成。
3. Docker、Windows、Linux 安装没有实测。本机没有 Docker，也没有 Windows 或 Linux 主机。
4. 控制台没有做浏览器级验证。页面渲染、点击流程、响应式布局、暗/亮主题只有代码、单元测试和数据接口冒烟的证据。
5. 没有推送。仓库没有配置远端；推送属于对外操作，需在确认远端状态与合并策略后进行（见第 8.2 节）。

已经确认的事实：

- 后端全量测试 2485 项通过，0 失败，P0 门禁 67/67。
- 控制台类型检查 0 错误，单元测试 15 项通过，生产构建成功，端点冒烟 32/32。
- Python SDK 10 项、TypeScript SDK 9 项测试通过。
- DSH 端到端演示在 Python 与 TypeScript 中各 16/16 通过（全新数据目录、真实服务）。
- 源项目（JarvisAI）迁移前后没有任何已跟踪文件改动（见第 8.3 节）。

---

## 1. 总体完成情况

### 1.1 阶段完成表

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| Phase 0 审计与净室复制 | 完成 | 审计摘要见附录 A；复制清单见第 2.3 节 |
| Phase 1 Console 基础与统一 API 底座 | 部分 | 统一错误信封、类型化 API 客户端、会话与主题、路由、布局与通用组件。未使用任务书要求的成熟第三方 UI 框架，组件为自研的最小集合；响应式 CSS 已写，未做浏览器验证 |
| Phase 2 Dashboard | 部分 | 数据来自 `/api/v1/console/dashboard`；图谱增长趋势由服务端标注为 Not Available。自动化任务趋势图未实现，也没有标注 Not Available；知识图谱卡片没有“最近图谱更新”，孤立节点以孤立关系代替 |
| Phase 3 Memory Explorer | 部分 | 列表、关键词搜索、类型与状态过滤、新建（经自动记忆治理）；标签、来源、时间范围、敏感度过滤标注为 Not Available。重复记忆识别、合并建议与合并界面未实现，也没有标注（合并接口已存在） |
| Phase 4 Memory Detail & History | 部分 | 详情、审计日志、待审批的遗忘与修改请求；JME 没有版本表，历史即审计日志；按记忆的访问历史与图谱关联标注为 Not Available |
| Phase 5 Knowledge Graph Explorer | 部分 | 节点列表与搜索、一度与二度关系视图、关系列表、同步状态、JSON 导出。手工编辑、路径查询、聚类、软失效执行未实现（Not Available）；图谱默认关闭 |
| Phase 6 Governance Center | 部分 | 概览、只读策略、候选/敏感/遗忘/修改审批、最近审计事件。规则对象的增删改、启停与规则测试未实现（Not Available） |
| Phase 7 Memory Spaces | 部分 | 每个非默认空间有独立数据库与向量目录；创建、归档、授权视图。空间详情未显示数据访问策略与记忆分类统计 |
| Phase 8 Agent Management | 部分 | 创建、启用与停用、删除、权限上限、空间授权、令牌颁发/轮换/撤销、访问记录。令牌只存 HMAC 摘要，明文只显示一次。界面未提供 Agent 名称与描述的编辑，也未显示 Agent 来源字段 |
| Phase 9 System Operations | 部分 | 组件状态、接口访问记录、健康、运行环境、图谱与 JMR 状态。任务队列、重试队列、死信队列未实现（Not Available） |
| Phase 10 Context & Retrieval Lab | 部分 | 检索测试、召回结果与相关度、排除统计、上下文块预览、召回历史（仅元数据）。逐项排序拆解与召回路径未实现（Not Available） |
| Phase 11 Unified API v1 & API 管理 | 部分 | `/api/v1` 前缀、认证、授权、统一错误格式、限流与 `Retry-After`、请求校验、访问日志、OpenAPI（`/openapi.json`、`/docs`） |
| Phase 12 SDK、集成与 DSH 教程 | 完成（DSH）；部分（其它框架） | Python 与 TypeScript SDK 覆盖 Remember、Recall、Forget、Update、Search、Context、Graph、Governance；DSH 教程与示例两种语言均 16/16；其它框架只有模板文档，未验证 |
| Phase 13 Backup & Data Management | 部分 | 备份创建、列表、验证（`VACUUM INTO` 与附加检查）；导出 JSON、CSV、图谱、审计；恢复只能离线执行；导入通过脚本完成，控制台没有导入页面；存储大小未显示 |
| Phase 14 Settings | 部分 | 界面偏好（主题、时区）；服务端设置只读展示；关于页。构建号标注为 Not Available。设置页中的语言、记忆保留策略与 Integration 分组未实现（集成信息见“SDK 与接入”页） |
| Phase 15 响应式与移动端 | 部分 | 断点、侧边栏抽屉与表格卡片化已写入 CSS；未在浏览器中验证 |
| Phase 16 安全审计 | 部分 | 两轮只读审查与一轮自查已完成；O-01（P1）未完全消除，按任务书第 16 节不宣称完成。见第 7 节。18 个安全面的状态见 `docs/SECURITY.md` |
| Phase 17 测试 | 部分 | 后端 2485、控制台 15、SDK 10 与 9、演示 16×2、冒烟 32/32。前端测试只覆盖工具函数、API 客户端、令牌与应用挂载，没有组件、路由与界面测试。8 个 E2E 场景中 4 个已覆盖、4 个部分覆盖（见第 6.3 节） |
| Phase 18 文档 | 完成 | 21 份核心文档已写入，另有安全审计、文档索引，以及从 OpenAPI 生成的 API 参考 |
| Phase 19 开源准备 | 部分（远端未配置，未推送） | MIT 许可证、贡献指南、行为准则、安全政策、变更日志、Issue 与 PR 模板、三个 CI 工作流（未运行）、版本 1.0.0；隐私扫描已做 |
| Phase 20 部署方案 | 部分 | Dockerfile、compose 文件、入口脚本与环境变量模板已写，未构建；macOS 已实际运行；Windows 与 Linux 文档未执行 |
| Phase 21 最终集成验收 | 未完成 | 30 项中已验证 12 项、部分 17 项、未验证 1 项（见第 6.4 节） |

### 1.2 未完成与部分完成的内容

- 独立审计（尚未进行）。
- 远端仓库与推送。
- Docker 镜像构建与运行；Windows 与 Linux 安装实测。
- 控制台浏览器级验证，响应式与主题视觉检查，移动端图谱交互。
- 图谱启用态的端到端实机演示（本次实机运行时图谱处于关闭状态）。
- Not Available 标注的功能（见第 3.4 节）。
- 界面中未实现、也未标注 Not Available 的任务书要求（见第 10 节第 15 项）。
- 源项目基线之后的若干修复未移植（见第 2.3 节）。
- 真实 DSH 应用接入（只有示例）。
- DeepSeek、OpenAI 兼容、Claude、OpenClaw、LangChain、LlamaIndex 适配器只有模板文档，未验证。

### 1.3 阻塞事项

1. 独立审计：尚未进行。
2. 推送：需要确认远端状态与合并策略（见第 8.2 节）。
3. Docker：本机没有 Docker，需要在有 Docker 的环境中构建并运行一次健康检查。

---

## 2. 架构、模块关系与零侵入证明

### 2.1 最终架构

```text
   所有者（浏览器控制台）                 Agent 与应用（DSH、DeepSeek、Claude、自定义 …）
   所有者凭据                           代理令牌 jmt_…，按空间授权
          |                                              |
          v                                              v
   /api/v1/{memories, console, control, jkg, jmr, mce, …}   /api/v1/agent/*
          |                                              |
          +------------------------+---------------------+
                                   v
                  JarvisMind 进程（FastAPI，单进程）
     JME  记忆权威（存储、检索、生命周期、审计）
     JMR  治理（凭据门禁、敏感保留、审批、非人类主体规则）
     JKG  图谱（由记忆派生；源记忆失效后，节点与关系不再返回）
     MCE  上下文（混合召回、相关度下限、不可信数据块）
                                   |
     control.db          控制面：空间、代理、授权、令牌摘要、访问日志（仅元数据）
     jarvismind.db       默认空间：JME + JMR + JKG 表（schema v6）
     spaces/<id>/        每个非默认空间：独立数据库与独立向量目录
```

### 2.2 Console 与各模块的关系

- Console 只调用公开的 HTTP API，不直接访问数据库，也不保存记忆副本。
- JME 是记忆的唯一权威来源，Console 的所有记忆操作都经过 JME 的服务层。
- JMR 负责治理。Console 的审批按钮调用服务端审批接口，由服务端决定是否执行。
- JKG 是派生视图。图谱关闭时接口返回 503 和状态，控制台显示“不可用”及缺失的表。
- MCE 负责召回与上下文块。上下文块用 `trust="untrusted-data"` 包裹，并转义标记字符。
- 依赖方向：JME 不导入 JKG（由 `tests/test_jkg_integration.py` 中的源码扫描测试检查）；控制台不导入后端代码；SDK 只使用 HTTP。

### 2.3 净室复制与零侵入证明

| 项目 | 内容 |
| --- | --- |
| 基线 | 源项目（JarvisAI）的一个已提交版本，只读导出 |
| 导出方式 | `git archive` 导出到源项目之外的暂存目录；源项目工作区没有被写入 |
| 初始复制 | 271 个文件：JME 应用代码（包名 `app` 改为 `jarvismind`）、JME 测试、运维脚本（备份、恢复、导出、v6 升级、JKG 安装器、导入脚本）、`requirements.txt`、`pytest.ini` |
| 排除 | `jme/data/`（数据库、令牌、授权密钥、向量索引、备份、日志）；备份目录；个人分析数据目录；`.git/`；个人数据文件（未复制）；仅用于局域网测试的路由与脚本；P0 门禁插件包；本地工具与调试脚本 |
| 重命名 | 包名 `app` 改为 `jarvismind`；环境变量前缀 `JME_` 改为 `JARVISMIND_`；令牌文件 `.jme_token` 改为 `.jarvismind_token`；数据库文件 `jme.db` 改为 `jarvismind.db` |
| 有意保留 | 导出格式标识 `jme-export` 与密码学域分隔串（如 `jme/p1-…`），以便与既有导出和密钥派生互通 |
| 未包含的基线之后改动 | 源项目在基线之后的若干修复与测试（JKG 软失效、MCE fail-closed、P1 激活来源、统一 E2E 测试）。这些改动不在本项目中。若要移植，需逐项引入并重新测试 |
| 未包含的工作区改动 | 源项目工作区中未提交的修改（不属于本项目） |

零侵入证明见第 8.3 节。本项目的 `.git` 是新建的，没有复制源项目的 `.git`，因此两个仓库的历史没有关联。

---

## 3. 功能

### 3.1 控制台页面（15 个页面文件）

仪表盘、记忆浏览器、记忆详情（含审计历史与审批）、上下文实验室、知识图谱、治理中心、记忆空间、Agent 管理、系统运维、API 管理、SDK 与接入、备份与导出、设置，以及登录页与 404 页。

### 3.2 统一 API v1

- 实际操作数：174 个（`docs/API-REFERENCE.md`，由服务端 OpenAPI 生成，19 个分组）。其中代理面 14 个，`/health` 1 个，所有者面 159 个。
- 代理面 `/api/v1/agent/*`（需要代理令牌）：

| 方法与路径 | 用途 | 所需权限 |
| --- | --- | --- |
| GET `/me` | 身份、权限、可用空间 | 无 |
| POST `/remember` | 经治理的记忆写入，返回回执 | write |
| POST `/extract` | 从对话中抽取候选（仍经治理） | write |
| POST `/recall` | 按查询召回排序结果 | search |
| GET `/search` | 关键词搜索 | search |
| POST `/context` | 上下文块与引用 | context |
| GET `/memories` | 列出可读记忆（新到旧） | read |
| GET `/memories/{id}` | 读取一条可读记忆 | read |
| POST `/memories/{id}/forget-requests` | 请求遗忘（由所有者决定，不返回令牌） | delete 与读写授权 |
| POST `/memories/{id}/modification-requests` | 修正请求（JMR 规则拒绝代理发起） | update |
| GET `/governance/requests` | 本代理发起的请求及状态 | read |
| GET `/graph/status` | 图谱在本空间是否可用 | graph |
| GET `/graph/nodes` | 源记忆仍有效的图谱节点 | graph |
| GET `/events` | 本代理自己的访问记录（仅元数据） | 无 |

- 所有者面：记忆（列表、搜索、读取、审计）、候选、遗忘与修改请求审批、生命周期（归档、恢复、合并预览与确认）、图谱、JMR（含调度器）、MCE、控制面（空间、代理、授权、令牌、访问日志）、控制台（仪表盘、活动、运维、治理概览、API 管理、设置、导出、备份、实验室）、嵌入与混合检索、索引。
- 统一错误格式：`{"error": {"code", "message", "details"}}`。`error.code` 表示类别，`details.code` 或 `details.reason` 表示具体原因。
- 限流：按进程计数，60 秒滑动窗口。所有者 600 次/分钟，代理令牌 120 次/分钟，失败认证 20 次/分钟（按客户端地址摘要计数）。超限返回 429 与 `Retry-After`。
- 访问日志：每个 `/api/` 请求记录一行元数据（方法、路由模板、状态、延迟、主体类型、代理 ID、令牌 ID、空间、错误码、客户端地址摘要）。不记录请求体、查询串、请求头或原始地址。保留最近 100 000 行。

### 3.3 能力清单

- 记忆：创建（经治理）、读取、列表、关键词搜索（启用向量时为混合检索）、归档与恢复、合并（预览、确认、拒绝）、两步遗忘、审计历史、直接编辑（默认关闭）。
- 治理：凭据拦截（回执 `DENIED`）、敏感内容保留（`PROTECTED`）、重复（`DUPLICATE`）、冲突（`CONFLICT`）、审批中心、非人类主体规则。
- 图谱：由记忆派生的节点与关系；源记忆失效后不再返回；初始化任务、同步、读取接口。
- 上下文：混合召回、相关度下限、上下文块、排除统计、引用。
- 空间与代理：物理隔离的空间、按空间授权、代理权限上限、令牌颁发、轮换、撤销与过期。
- 备份与导出：在线一致性备份（`VACUUM INTO`）与验证；导出记忆（JSON、CSV，最多 5000 行）、图谱（JSON，最多 1000 节点与 1000 条边）、审计（JSON），默认脱敏。
- SDK 与 DSH 示例。

### 3.4 Not Available（服务端不提供，控制台如实标注）

- 标签、来源、时间范围与敏感度的列表过滤；记忆分类（类型）的修改；批量删除与批量编辑。
- 按记忆的关联记忆、访问历史、治理记录汇总。
- 图谱的手工编辑、最短路径、聚类、重复节点与重复关系报告、软失效预览与执行、截图导出。
- 治理规则对象（增删改、版本、启停、规则测试、执行记录）。
- 任务队列、重试队列、死信队列、JME 同步状态、MCE 索引同步状态、日志流、存储与索引容量检查。
- 构建号；控制台内的导入页面（导入通过脚本与生命周期导入接口完成）。

---

## 4. UI

- 技术栈：React 19.3.1、react-dom 19.3.1、React Router 7.18.6、TypeScript（strict）、Vite 8.3.8、Vitest 5.1.1（jsdom）。未使用任务书要求的成熟第三方 UI 组件库；通用组件为项目内自研的最小集合。
- 主题：深色为默认，可切换为浅色；偏好保存在 `localStorage`。
- 响应式：断点为 1100 px、900 px 和 640 px。窄屏时侧边栏变为抽屉，表格行变为卡片式布局。这些规则已写入 CSS，但没有在浏览器中验证。
- 通用组件：按钮、页头、卡片、徽标、Not Available 标记、统计卡、空状态、错误状态、加载状态、提示条、表单字段、对话框、标签页、分页、数据表、柱状图、横向条形图、键值列表、代码块、Toast、错误边界。
- 数据来源：页面只显示 API 返回的数据。服务端不提供的数字标注为 Not Available，不估算、不伪造。
- 令牌：所有者令牌只保存在标签页的 `sessionStorage` 中，关闭标签页即失效。修改请求与遗忘请求的一次性确认令牌只保存在内存中，刷新页面后消失。
- 验证：`tsc --noEmit` 0 错误；`vitest run` 4 个文件、15 项用例通过；`vite build` 成功；控制台调用的 51 个 `/api/v1` 路径全部存在于服务端 OpenAPI（已核对）。
- 未验证：浏览器渲染与点击流程；窄屏与平板的实际显示；暗/亮主题的视觉效果；移动端图谱交互。

---

## 5. 后端

### 5.1 认证与授权

- 所有者凭据：环境变量 `JARVISMIND_API_TOKEN`，或数据目录中的 `.jarvismind_token`（首次启动时生成，权限 0600）。比较使用常数时间函数。
- 代理令牌：格式为 `jmt_<公开标识>_<密钥>`。服务端只保存 HMAC-SHA256 摘要，摘要密钥为 `.control_key`（32 字节，以独占方式创建，权限 0600）。明文只在颁发或轮换时返回一次。有效期 1 至 3650 天，默认 90 天。轮换沿用原有效期，并立即吊销旧令牌。
- 每个请求的顺序：认证，然后选择空间（请求头 `X-JarvisMind-Space`，或代理的默认空间），然后做权限与授权检查，最后处理。无法选择空间的请求被拒绝，不会回退到默认空间。
- 所有者路由只接受所有者凭据。代理路由拒绝所有者凭据（403，`permission_denied`，`details.reason = agent_credential_required`）。
- 代理的权限是上限，令牌可以收窄但不能扩大。停用代理、删除代理、吊销令牌或令牌过期后，访问立即失败。
- 未知空间与无权限空间返回相同的结果，防止枚举空间名称。

### 5.2 权限集合（8 个）

`read`、`search`、`context`、`write`、`delete`、`update`、`graph`、`sensitive`。新代理默认只有 `read`、`search`、`context`。`sensitive` 默认不授予。`update` 对应的修正请求被 JMR 非人类主体规则拒绝，仅作为契约保留。

### 5.3 数据结构、数据库与迁移

- 默认空间数据库 `jarvismind.db`（JME、JMR、JKG 表，schema v6）。主要表包括 `memory_records`（记忆）、`memory_audit_logs`（审计日志）、`memory_candidates`（候选）、`memory_sources`（来源）、`system_settings`（键值设置，包含 `jkg.enabled`）。图谱使用 `jkg_*` 三张表与九个索引。另有遗忘请求、修改请求与 JMR 派生表。
- 控制库 `control.db`：6 张表（包括 `control_meta` 与 `memory_spaces`，以及代理、授权、令牌摘要和访问日志），`control_meta` 记录控制面 schema 版本。
- 每个非默认空间：同样的 schema v6 数据库，加上独立的向量目录。
- 迁移策略：启动时不建表、不迁移。`init-db` 只创建全新数据库，遇到已有表即拒绝；`serve` 在未初始化时拒绝启动；旧版本升级由 `scripts/upgrade_to_v6.py` 显式完成；图谱由 `scripts/upgrade_to_jkg.py` 显式安装（默认只检查、不写入）。请求处理代码中没有 DDL。
- SQLite：WAL 模式，外键开启，忙等 5000 毫秒。控制库与空间库使用相同设置。
- 数据目录权限：`init-db` 与 `serve` 把进程 umask 设为 077。在全新目录上验证：目录为 0700，数据库与令牌文件为 0600。

### 5.4 Worker 与后台任务

- 没有后台线程、定时器或守护进程。
- 图谱 Worker：`POST /api/v1/jkg/worker/run` 执行一次拉取。需要 `JARVISMIND_JKG_WORKER_ENABLED=1`，默认关闭。使用租约（默认 300 秒），防止并发运行。
- 图谱同步：`POST /api/v1/jkg/sync/tick` 处理一批审计日志；`POST /api/v1/jkg/sync/reconcile` 按键集分页重新核对源记忆。
- JMR 调度器：拉取式，状态与触发接口为 `GET /api/v1/jmr/scheduler` 与 `POST /api/v1/jmr/scheduler/tick`，实际执行仍经 JMR 服务。

### 5.5 备份与恢复

- 创建：`POST /api/v1/console/backups`，在所选空间的 `backups/` 目录中用 `VACUUM INTO` 生成一致性快照。文件名为 `jarvismind-时间戳.db`，同一秒内的第二个文件带序号后缀。
- 列表与验证：`GET /api/v1/console/backups` 与 `POST /api/v1/console/backups/verify`。验证只读取文件，做完整性检查并统计记忆行数。备份名称经固定模式校验，防止路径穿越。
- 恢复：只能离线执行，由运维人员运行 `scripts/restore_db.py`（先校验，`--force` 时先保存安全副本）。没有恢复的 API，代理不能触发恢复。

---

## 6. 测试

### 6.1 测试结果（最终状态）

| 项目 | 命令 | 结果 | 证据文件（本地） |
| --- | --- | --- | --- |
| 后端全量（含 P0 门禁） | `python -m pytest`（在 `backend/` 中） | 2485 passed，0 failed，0 skipped，1 个 warning；P0 门禁 collected=67 executed=67 passed=67 failed=0 collect_only=False；耗时 275.5 秒，退出码 0 | `.scratch/release-check/pytest-final.log` |
| 控制台类型检查 | `npm run typecheck` | 0 错误 | `.scratch/tsc2.log` |
| 控制台单元测试 | `npm test`（vitest） | 4 个文件，15 项用例通过 | `.scratch/vitest2.log` |
| 控制台生产构建 | `npm run build` | 成功 | `.scratch/build2.log` |
| Python SDK | `python -m pytest tests` | 10 passed | `.scratch/sdk-python-tests2.log` |
| TypeScript SDK | `npm test`（先构建，再 `node --test`） | 9 pass，0 fail | `.scratch/sdk-ts-tests2.log` |
| 控制台端点冒烟 | `.scratch/smoke_console.py`，逐个调用 32 个端点（含 `/health`、一次 POST 实验室接口） | 32/32 返回 2xx。`GET /api/v1/jkg/overview` 在图谱关闭时返回 503，这是设计行为，脚本按预期记录 | `.scratch/release-check/smoke-final.log` |
| DSH 演示（Python） | `demo_cross_session.py`，全新数据目录，真实服务 | 16/16 checks passed | `.scratch/release-check/demo-py-final.log` |
| DSH 演示（TypeScript） | `node examples/dsh-integration/typescript/demo.mjs` | 16/16 checks passed | `.scratch/release-check/demo-ts-final.log` |
| API 参考生成 | `python scripts/generate_api_reference.py` | 174 个操作，19 个分组 | `.scratch/release-check/apiref-final.log` |
| 文档一致性 | `.scratch/doccheck.py`（路由、环境变量名、错误码） | 0 个问题；检查 35 个文档、157 个 API 路径、139 个环境变量名 | `.scratch/release-check/doccheck-final2.log` |
| 相对链接 | `.scratch/linkcheck.py`（全部 Markdown） | LINKS_CHECKED=108，BROKEN_COUNT=0 | 见附录 B |

说明：

- 全量测试中的 1 个 warning 是 Starlette 的弃用提示（测试客户端依赖 httpx），不是失败。
- P0 门禁是从源项目继承的测试机制：它按名称统计 67 个指定用例，要求全部被收集，因此部分运行会被中止。它不是 JarvisMind 1.0 新设计的门禁。
- 测试在 `PYTHONDONTWRITEBYTECODE=1` 下运行，并使用 `-p no:cacheprovider`，不在仓库中留下缓存文件。

### 6.2 覆盖范围

- 后端（2485 项，含基线）：JME 的记忆、搜索、候选、遗忘、修改、生命周期、来源、概览、混合检索；JMR 治理（凭据门禁、敏感度、隐私、安全、成本治理、调度器）；JKG 图谱（抽取、匹配、质量、初始化、生命周期同步、Worker、API、集成）；MCE 上下文（召回、预算、重构、API、JKG 集成、治理）；启动安全与数据库迁移门禁；备份与升级脚本。
- 本次新增的 5 个测试文件（61 项）：

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `tests/test_control_plane.py` | 23 | 空间、代理、授权、令牌颁发、轮换、撤销、过期、禁用、访问日志 |
| `tests/test_agent_facade.py` | 13 | 代理接口权限、敏感记忆拒绝、代理无法自行批准遗忘请求 |
| `tests/test_space_isolation_and_console.py` | 14 | 空间数据隔离、各空间独立向量目录、覆盖项不继承、概览缓存按容器隔离、导出脱敏、设置不泄露密钥、API 管理、备份与路径穿越拒绝、CLI init-db |
| `tests/test_console_api.py` | 4 | 控制台静态托管与深链回退、安全响应头、Context Lab 仅所有者可用 |
| `tests/test_security_followups.py` | 7 | 代理不能覆盖所有者的待审批请求、敏感元数据、按空间备份、令牌有效期、被使用中的空间不可归档、被拒读取不更新访问时间、元数据扫描规则 |

- 控制台（15 项）：格式化工具、API 客户端（认证头、错误信封、校验错误信息）、一次性确认令牌、应用挂载与登录页渲染。
- SDK：Python 10 项（本地存根服务器）；TypeScript 9 项（`node:test`）。

### 6.3 8 个端到端场景（任务书第 17 节第 7 条）

| 编号 | 场景 | 证据 | 状态 |
| --- | --- | --- | --- |
| 1 | 创建记忆 → 控制台展示 → 详情 → 修改 → JME 数据正确 | 冒烟覆盖列表、详情与审计端点；演示创建记忆；修改通过修改请求并经审批，有测试覆盖。控制台的点击流程未自动化，未在浏览器中验证 | 部分 |
| 2 | 创建记忆 → JKG 自动关联 → 控制台图谱展示 | 基线 JKG 测试（`test_jkg_*`）覆盖抽取与同步。实机运行时图谱关闭，控制台显示“不可用”及缺失的表。图谱启用态与图谱页面未实机验证 | 部分 |
| 3 | 删除记忆 → JME 状态正确 → JKG 正确处理 → MCE 不再召回 | 实机：审批后代理读取详情返回 404；上下文与搜索不再返回该记忆；新会话不召回（演示的会话 C 三项全部通过）。JKG 启用态未实机运行 | 部分 |
| 4 | 创建 Agent → 创建令牌 → Agent 查询记忆 → 权限验证 | `test_agent_facade.py`（13 项）、`test_control_plane.py`（23 项）。实机：`setup_agent.py` 创建代理与令牌，演示完成查询，修改请求返回 403 | 已覆盖 |
| 5 | 敏感记忆 → JMR 处理 → 未授权访问被拒绝 | `test_security_followups.py::test_sensitive_metadata_is_withheld_from_agents_and_exports`。实机：手机号返回 PROTECTED，响应中不出现号码 | 已覆盖 |
| 6 | Worker 异常 → 控制台显示 → 重试 → 恢复 | `test_jkg_worker.py`（29 项，含租约与失败路径）。控制台运维页显示 Worker 状态，但没有控制台级的异常与恢复演示 | 部分 |
| 7 | 数据导出 → 数据正确 → 敏感数据遵循治理 | `test_space_isolation_and_console.py::test_export_redacts_sensitive_text_by_default`、`test_export_csv_has_header_and_no_secret_token`。实机：记忆 JSON/CSV、图谱、审计导出均返回 200 | 已覆盖 |
| 8 | 跨记忆空间 → 权限隔离 → 数据不泄露 | `test_memory_written_to_one_space_is_invisible_in_another`、`test_each_space_has_its_own_database_and_vector_directory`、`test_shared_qdrant_override_is_not_inherited_by_other_spaces`。审查中的探针复现了跨空间的 403/404 一致性 | 已覆盖 |

### 6.4 任务书第 21 节 30 项验收对照

状态含义：已验证 = 有自动化测试、冒烟或实机演示证据；部分 = 有部分证据，但某个界面或子项未验证；未验证 = 没有证据。

| 编号 | 验收项 | 状态 | 依据或缺口 |
| --- | --- | --- | --- |
| 1 | 源项目零修改、零移动、零删除 | 已验证 | 第 8.3 节的只读检查 |
| 2 | Console 能正常启动 | 部分 | 服务端托管 `/console/`，深链回退正常，应用挂载测试通过；浏览器渲染未验证 |
| 3 | 后端统一 v1 API 正常 | 已验证 | 174 个操作；冒烟 32/32；测试 2485 项 |
| 4 | JME 正常 | 已验证 | 基线 JME 测试；演示中的创建、读取、遗忘 |
| 5 | JMR 正常 | 已验证 | 治理测试；演示中的敏感保留与非人类主体规则 |
| 6 | JKG 正常 | 部分 | 基线 JKG 测试与 Worker 测试；实机运行时图谱关闭 |
| 7 | MCE 正常 | 已验证 | 召回与上下文测试；演示中会话 B 的召回与不可信数据标记 |
| 8 | Dashboard 数据正常 | 部分 | 接口 200 与测试；页面未在浏览器中验证 |
| 9 | Memory Explorer 正常 | 部分 | 列表、搜索接口冒烟通过；页面未在浏览器中验证 |
| 10 | Memory Detail 正常 | 部分 | 详情、审计接口冒烟通过；页面未在浏览器中验证 |
| 11 | Memory Edit 正常 | 部分 | 直接编辑默认关闭（配置项）；修改请求路径有测试；页面未验证 |
| 12 | Memory Delete 正常 | 部分 | 两步遗忘实机演示通过；页面未验证 |
| 13 | Graph Explorer 正常 | 部分 | 图谱关闭时的不可用状态有数据证据；启用态与页面未验证 |
| 14 | Governance 正常 | 部分 | 审批接口与测试；页面未验证 |
| 15 | Agent Management 正常 | 部分 | 代理与令牌测试、实机创建；页面未验证 |
| 16 | Memory Space 正常 | 已验证 | 空间隔离测试 14 项；探针复现 |
| 17 | System Operations 正常 | 部分 | 运维接口冒烟通过；队列与死信队列为 Not Available |
| 18 | Context Lab 正常 | 部分 | 实验室接口测试与冒烟；页面未验证 |
| 19 | API Management 正常 | 部分 | API 管理接口测试与冒烟；页面未验证 |
| 20 | SDK 正常 | 已验证 | Python 10 项、TypeScript 9 项；演示 |
| 21 | Integrations（含 DSH 示例）正常 | 已验证 | 示例两种语言 16/16；真实 DSH 应用未接入 |
| 22 | Backup 正常 | 已验证 | 备份与验证测试；冒烟；恢复为离线脚本 |
| 23 | Settings 正常 | 部分 | 设置接口冒烟与测试；页面未验证 |
| 24 | Responsive 正常 | 未验证 | 只有 CSS 断点，没有浏览器检查 |
| 25 | Authentication 正常 | 已验证 | 认证测试；审查探针；服务启动后的未认证请求返回 401 |
| 26 | Authorization 正常 | 已验证 | 授权测试；审查探针；代理访问所有者路由返回 403 |
| 27 | Tests 全部执行 | 已验证 | 第 6.1 节全部命令 |
| 28 | Security Audit 完成 | 部分 | 两轮只读审查与自查已完成；独立审计尚未通过；O-01 为残余 P1 |
| 29 | Build 正常 | 部分 | 控制台构建、SDK 构建、后端测试通过；Docker 镜像未构建 |
| 30 | 独立仓库 Git 状态与提交历史干净正常 | 部分 | 本地 6 个提交，工作区干净；尚未创建 `v1.0.0` 标签；未推送（无远端） |

统计：已验证 12 项，部分 17 项，未验证 1 项。

---

## 7. 安全

### 7.1 审查方法

1. 第一轮：独立审计子代理，只读。用项目外的探针脚本在临时目录中驱动真实应用，并报告证据。审查过程没有修改仓库。
2. 第二轮：另一个只读审计子代理，使用一个 DeepSeek 系列的模型（经授权）。方法相同。
3. 第三轮：自查。在文档一致性检查中发现两处真实的绕过，并核对了文档中的安全声明。
4. 每次修复后运行完整测试套件，并为每个修复补充测试或探针。

这些审查不包括渗透测试、模糊测试、依赖漏洞扫描和部署环境审查，这些都列为开放项。

### 7.2 结果

| 轮次 | P0 | P1 | P2 | P3 | 处理 |
| --- | --- | --- | --- | --- | --- |
| 第一轮（控制面、空间、令牌、访问） | 1 | 2 | 9 | 5 | P0 与 P1 全部修复。P2 中 2 项登记为开放项（O-02 匿名访问日志写入、O-03 代理后的认证限流），其余已修复。P3 中 1 项登记为开放项（O-06），其余已修复 |
| 第二轮（敏感元数据、代理覆盖、备份、时序） | 0 | 2 | 2 | 4 | 除 R2-09（仅代码核实，登记为 O-08）外全部修复 |
| 第三轮（自查：元数据绕过、文档与实现不一致、导出、umask） | 0 | 2 | 2 | 6 | 全部修复或修正文档（R3-01 至 R3-10） |

典型修复：

- 第一轮 P0：代理可以通过自填标签 `agent:` 让所有者路由在无令牌时批准遗忘。修复：`agent:` 前缀由服务端保留，公开路由拒绝；无令牌的批准只对服务端记录的代理来源生效。
- 第一轮 P1：控制面未接入应用，所有者令牌被用于数据路由；空间创建失败时残留存储。修复：`main.py` 装配控制面，数据路由改用主体鉴权；`provision_new` 只创建全新存储，失败时清理自己的残留。
- 第二轮 P1：元数据中的敏感文本只检查了内容字段；代理可以通过重复请求覆盖所有者的待审批令牌。修复：检查元数据的键与值；代理路径从不轮换令牌，所有者路径不替换代理的待审批请求（409）。
- 第三轮 P1：元数据中的数字不被检查；identifier 键下的值完全不扫描。修复：数字按文本检查；identifier 键下只豁免系统生成的精确格式（32 位十六进制、UUID、前缀加 16 位十六进制）。
- 第三轮 P2：图谱关闭时，导出接口返回 503。修复：返回空结果并标注图谱状态。

### 7.3 开放项

| 编号 | 级别 | 内容 | 当前缓解 | 计划 |
| --- | --- | --- | --- | --- |
| O-01 | P1（已收窄） | identifier 键下的值只豁免精确的系统生成格式，其余值照常检查。残余风险：恰好是三种格式之一且含敏感文本的值。 | 凭据门禁与敏感规则仍然适用于其余内容 | 在独立审计中确认残余风险；新增标识符格式时同步补测试 |
| O-02 | P2 | 匿名请求也会写访问日志，每个请求一次数据库写入。 | 日志只含元数据，保留 100 000 行 | 对匿名请求不写行，或改为缓冲写入 |
| O-03 | P2 | 限流与认证失败计数按套接字地址，代理后的所有客户端共享一个桶。 | 在反向代理处另外限流；不要直接暴露服务 | 信任已配置的代理转发地址，并按凭据限流 |
| O-04 | P2 | 备份与导出是明文 SQLite 与 JSON。 | 文件权限与运维存储控制 | 可选的、由运维持有密钥的加密 |
| O-05 | P2 | 敏感度策略是规则集，不是完整分类器。 | 命中的内容被保留；未命中不代表安全 | 为每个类别补充测试，并利用所有者的决定做反馈 |
| O-06 | P3 | 遗忘令牌“消费后清除摘要”的文档与实现不一致（实际只标记为已消费）。 | 重放被状态与已消费标记拒绝 | 统一措辞，或在消费时清除摘要 |
| O-07 | P3 | 缺少专门测试覆盖“重新启用归档空间且存储缺失”的路径（代码路径存在）。 | 代码路径由 `ensure_provisioned` 保证 | 补充测试 |
| O-08 | P2 | 图谱有效性过滤与合并记忆的代理拒绝，仅通过阅读代码核实。 | 代码审查 | 在启用图谱的测试环境中运行 |
| O-09 | P3 | 没有运行依赖漏洞扫描。 | 依赖使用 `requirements.txt` 中的最低版本约束 | 在 CI 中加入扫描 |
| O-10 | P3 | Docker、Windows、Linux 部署未实际运行。 | 配置已文档化并审查 | 在每个平台上验证后再用于生产 |

按任务书第 16 节的规则，O-01 仍视为未完全解决的 P1，因此本版本不宣称安全审计完成。

### 7.4 18 个安全面

每一项的状态见 `docs/SECURITY.md`。需要特别注意的是：API 令牌（O-03、O-06）、敏感数据（O-01、O-05）、备份（O-04）、限流（O-03）。

### 7.5 零个人数据与密钥扫描

- 扫描范围：全部提交的内容与提交元数据（`git rev-list --all`）。改写后历史仅含 6 个提交，旧对象已通过 `gc --prune=now` 清除。本地证据目录 `.scratch/` 未入库。
- 个人标识（操作系统账户名、个人邮箱域名、真实手机号）：0 处。合成样例保留，例如手机号 `13800138000` 与 `example.com`、`.invalid` 域名。
- 内部项目名、内部路径与内部文件名（源项目的仓库名与目录名、个人数据集名称）：0 处。仅第 2.3 节与第 8.3 节为满足任务书第 31 节的零侵入证明提到源项目名称，不含提交哈希、路径或文件清单。导入脚本已更名为 `import_memories.py`，不再带有个人数据集的名称。
- 密钥形态字符串：测试中的 `sk-` 样式字符串是合成值（如 `sk-abcdefgh…`）。AWS、GitHub 令牌与 PEM 私钥头形态的夹具已改为相邻字面量拼接（运行时值不变），仓库中不再出现完整字面量。推送前仍建议让托管平台的密钥扫描器复核。
- 令牌、密钥文件、数据库、日志、`.env` 文件：仓库内 0 个（`.env.example` 是空模板）。`.scratch/` 中的测试令牌与测试数据库已删除，只保留日志。
- 扫描在改写历史与本报告最终修改之后进行。

---

## 8. Git

### 8.1 提交（分支 `main`，本地）

| 哈希 | 时间（+0800） | 提交信息 | 文件数 |
| --- | --- | --- | --- |
| `76470bf` | 2026-11-22 03:33:48 | chore: add repository metadata, CI workflows and container files | 18 |
| `609f162` | 2026-11-22 03:34:02 | feat(backend): add the JarvisMind memory server | 302 |
| `180e046` | 2026-11-22 03:34:02 | feat(console): add the JarvisMind Console | 40 |
| `4d3b6e0` | 2026-11-22 03:34:02 | feat(sdk): add agent clients and the DSH integration example | 17 |
| `3b34fd8` | 2026-11-22 03:34:02 | docs: add the 1.0.0 documentation set | 22 |
| 最后一个提交（本报告） | 以 `git log` 为准 | docs: add the 1.0.0 release report | 1 |

说明：

- 每组提交都使用显式文件列表（`git ls-files --others --exclude-standard -- <目录>` 的输出交给 `git add --`），没有使用 `git add .` 或 `git add -A`。
- `.gitignore` 排除了 `.scratch/`、`backend/.venv/`、`console/node_modules/`、`console/dist/`、`sdk/typescript/dist/` 和数据目录。
- 尚未创建标签 `v1.0.0`。版本标签应在独立审计通过后再打，避免在未完成的状态上宣称发布。
- 改写记录：六个提交的作者与提交者已统一为项目所有者的身份，提交日期保持原样。`f41d6f4` 的说明去掉了源项目的基线哈希。改写只改动测试夹具的字面量写法、一处合成的本机路径样例、导入脚本的名称与本报告；测试行为不变，全量测试在改写后重新运行。

### 8.2 远端与推送

- 未配置远端（`git remote -v` 为空），未推送，未创建标签。
- 作者身份：六个提交的作者与提交者已改为项目所有者的个人身份（见提交元数据），占位身份已移除。改写只针对本地未推送的提交；内容中的源项目哈希、路径、文件清单与供应商标识已一并移除。
- 推送前需要确认远端状态与合并策略。在确认之前不做强制推送，也不删除远端内容。

### 8.3 源项目零变更证明

检查时间：2026-11-22（本机时间，+0800）。检查方式只读：`git status`、`git diff`、`git log`、`git rev-parse`，均带 `--no-optional-locks`。

- 迁移前：源项目的 `git status --porcelain` 只显示本项目目录（未跟踪）。
- 迁移前后：`git diff` 为空，没有任何已跟踪文件被修改、移动或删除。
- 迁移期间源项目出现了不属于本项目的提交。本项目的操作没有对源项目执行写操作，也没有写入源项目目录。
- 复制来源是源项目的只读导出（`git archive`），导出目标位于源项目之外。

---

## 9. 部署

### 9.1 启动（已在 macOS 上执行）

环境：macOS，Python 3.13.18（由 uv 安装，虚拟环境位于 `backend/.venv`），控制台构建使用 Node v26.13.1 与 npm 11.20.0。

```bash
cd backend
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m jarvismind init-db        # 创建 data/jarvismind.db 与 data/control.db
python -m jarvismind serve          # 默认 http://127.0.0.1:8777
```

控制台：

```bash
(cd ../console && npm ci && npm run build)
JARVISMIND_CONSOLE_DIR="$(cd ../console && pwd)/dist" python -m jarvismind serve
```

所有者令牌位于 `data/.jarvismind_token`（首次启动时生成，权限 0600），也可以用环境变量 `JARVISMIND_API_TOKEN` 指定。

验证：`curl -s http://127.0.0.1:8777/health` 返回 `{"status":"ok","version":"1.0.0","database_connected":true,"schema_version":6}`。

### 9.2 环境变量

完整的运维变量与默认值见 `docs/CONFIGURATION.md`。其中的默认值与取值范围已对照 `backend/jarvismind/config.py` 核对。JMR、P1、MCE、JKG 的内部变量列在文档的“内部与高级变量”一节，它们的名称与默认值以源码为准。

### 9.3 Docker Compose

- 已编写：`Dockerfile`（两阶段：Node 20 构建控制台，Python 3.13 slim 运行）、`docker-compose.yml`（只发布 `127.0.0.1:8777`，命名卷 `jarvismind-data`，健康检查）、`docker/entrypoint.sh`（数据库缺失时执行 `init-db`）、`.env.example`、`.dockerignore`。
- 状态：未构建、未运行。本机没有 Docker。CI 的 `build.yml` 工作流也未运行。

### 9.4 DSH 接入教程验证

- 教程：`docs/tutorials/INTEGRATE-WITH-DSH.md`；示例：`examples/dsh-integration/`（设置脚本、Pre-Chat 与 Post-Chat 钩子、Python 与 TypeScript 演示）。
- 实机结果：Python 演示 16/16，TypeScript 演示 16/16。均在全新数据目录与本机服务上运行，覆盖跨会话召回、两步遗忘、遗忘后不召回、敏感内容保留、代理不能修正记忆。
- 未验证：真实 DSH 应用的接入。教程中的步骤已与示例逐一对应，但没有连接真实的 DSH 代码。

---

## 10. 剩余事项（真实未完成内容）

1. 独立审计：任务书要求的 Audit Agent 审查尚未完成。
2. 远端与推送：需要确认远端状态与合并策略，然后推送。
3. Docker：镜像未构建，容器未运行健康检查。
4. Windows 与 Linux：安装步骤是根据依赖与配置写成的，没有在对应主机上执行。
5. 控制台浏览器级验证：页面渲染、点击流程、响应式布局、暗/亮主题、移动端图谱交互。
6. E2E 场景 1、2、3、6 只有部分覆盖（见第 6.3 节）。
7. 源项目基线之后的若干修复未移植（JKG 软失效、MCE fail-closed、P1 激活来源、统一 E2E 测试等）。是否移植需要决定。
8. 真实 DSH 应用的接入；DeepSeek、OpenAI 兼容、Claude、OpenClaw、LangChain、LlamaIndex 适配器的实际验证。
9. Not Available 功能（第 3.4 节）。
10. 安全开放项 O-01 至 O-10（第 7.3 节）。其中 O-01 为残余 P1。
11. 测试框架中的 P0 门禁是基线继承的机制，其 67 个用例名称来自基线。
12. 依赖：测试客户端的 `StarletteDeprecationWarning` 提示 httpx 用法已弃用，升级依赖时需要处理。
13. 代码注释中仍有来自基线的任务编号（如 P0-03A、JME-x），不影响行为，但会让新读者困惑。
14. 图谱启用态下的控制台页面与代理接口没有在实机演示中运行。
15. 界面未实现、也未标注 Not Available 的任务书要求，需要在下一轮补齐并标注：重复记忆识别与合并界面（合并接口已存在）；Dashboard 的自动化任务趋势图与“最近图谱更新”；API 管理页的令牌列表（令牌位于 Agent 管理页）；备份页的存储大小与数据导入界面；设置页的语言、记忆保留策略与 Integration 分组；空间详情的数据访问策略与记忆分类统计；Agent 名称与描述的编辑，以及 Agent 来源字段；前端组件、路由与界面测试。

---

## 11. 下一阶段建议

1. 以独立审计结论为先：根据审计意见处理 O-01，并决定是否接受残余风险。
2. 在有 Docker 的机器上构建镜像，运行 `docker compose up` 与健康检查，把结果写回本报告。
3. 在 Windows 与 Linux 主机上各执行一次安装文档中的步骤。
4. 为控制台加入浏览器级测试（如 Playwright）：覆盖登录、记忆列表与详情、两步遗忘、审批中心、图谱（启用与关闭两种状态）、响应式布局与暗/亮主题。
5. 决定是否移植基线之后的源项目修复（JKG 软失效、MCE fail-closed），并在本项目中重新测试。
6. 启用图谱的端到端演示：按 `docs/KNOWLEDGE-GRAPH.md` 开启图谱，运行初始化与同步，验证场景 2、3、6。
7. 补全安全开放项：匿名访问日志（O-02）、代理感知限流（O-03）、备份加密（O-04）、依赖漏洞扫描（O-09）。
8. 把 Not Available 中用户最需要的项（标签与时间过滤、图谱路径查询、治理规则对象）排入 1.1。

---

## 附录 A：Phase 0 审计摘要（任务书第六节的七项）

**1. 已实现的功能（基线版本）**

- JME：记忆的创建、读取、更新，搜索（关键词与混合检索），生命周期（归档、恢复、过期），合并，两步遗忘，审计日志，来源，概览。
- JMR：凭据门禁，敏感度保留，候选审批，遗忘与修改请求审批，非人类主体规则，调度器，成本治理层。
- JKG：由记忆派生的图谱，安装器（默认只检查），Worker，生命周期同步，查询接口。
- MCE：混合召回，相关度下限，排除统计，上下文块。
- P1 统一治理门禁：默认关闭。
- 数据库结构版本 6。

**2. 可以直接复用的 API**

基线的记忆、搜索、候选、遗忘、修改、生命周期、来源、概览、嵌入、混合检索、JKG、JMR、MCE 接口。这些接口保持原有行为，并放在所有者面下。

**3. 需要新增并统一为 v1.0 的 API 功能**

- 代理面 `/api/v1/agent/*`：按权限和空间授权保护，只返回经治理的结果，遗忘请求不返回令牌。
- 控制面 `/api/v1/control/*`：空间、代理、授权、令牌、访问日志。
- 控制台面 `/api/v1/console/*`：仪表盘、活动、运维、治理概览、API 管理、设置、导出、备份、Context Lab。
- 统一错误格式、限流（含 `Retry-After`）、空间请求头、访问日志。

**4. 需要在 JarvisMind 中补齐的功能**

- 全新数据库的创建。基线的 `init_db.py` 处于第一阶段禁用状态，因此改为 `init-db` 的全新建库流程，已有表时拒绝。
- 启动入口 `python -m jarvismind`（`init-db`、`serve`）。
- 空间容器与控制库；每个空间独立的数据库与向量目录。
- Web 控制台、Python 与 TypeScript SDK、DSH 示例、文档、CI 与容器文件。

**5. 数据结构限制、安全限制与运行限制**

- 数据结构：基线没有空间字段，因此选择物理隔离（每个空间独立数据库），而不是改写所有查询。没有版本表，历史依赖审计日志。没有标签和时间范围字段。
- 安全：基线只有一个共享令牌，没有代理、授权或分级权限。敏感度依赖规则集。
- 运行：SQLite 单写者；限流按进程计数；没有后台服务，Worker 需要显式调用。

**6. 技术栈与推荐**

- 后端：Python 3.13、FastAPI、SQLAlchemy 2、Pydantic v2、SQLite（WAL）、嵌入式 Qdrant、fastembed（可选，默认模型 `BAAI/bge-small-zh-v1.5`）。
- 控制台：React 19、React Router 7、TypeScript、Vite 8、Vitest 5。
- SDK：Python 标准库；TypeScript 使用全局 `fetch`，没有运行时依赖。

**7. 复制清单与净室脱敏方案**

见第 2.3 节。

---

## 附录 B：验证命令与证据

以下日志保存在 `JarvisMind/.scratch/`（仅本地，不进入仓库）：

- 后端：`.scratch/release-check/pytest-final.log`（最终状态，2485 passed）；`.scratch/run16.log`（修复后的第一次全量运行，代码与最终状态一致）。
- 控制台：`.scratch/tsc2.log`、`.scratch/vitest2.log`、`.scratch/build2.log`。
- SDK：`.scratch/sdk-python-tests2.log`、`.scratch/sdk-ts-tests2.log`。
- 冒烟：`.scratch/release-check/smoke-final.log`。
- 演示：`.scratch/release-check/demo-py-final.log`、`.scratch/release-check/demo-ts-final.log`。
- API 参考：`.scratch/release-check/apiref-final.log`。
- 文档一致性：`.scratch/release-check/doccheck-final2.log`。
- 相对链接：`.scratch/linkcheck.py` 的输出，LINKS_CHECKED=108，BROKEN_COUNT=0。

主要命令（工作目录为 `JarvisMind/`）：

```bash
cd backend && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -rfE --tb=short
cd console && npm run typecheck && npm test && npm run build
cd sdk/python && python -m pytest tests
cd sdk/typescript && npm test
cd backend && .venv/bin/python scripts/generate_api_reference.py --base-url http://127.0.0.1:8777 --out ../docs/API-REFERENCE.md
```
