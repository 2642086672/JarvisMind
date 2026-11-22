# PHASE 1 报告：JARVISMIND-DSH-PLUGIN-01（子节点 1）

- **任务编号**：`JARVISMIND-DSH-PLUGIN-01`
- **阶段**：Phase 1（插件实现 + `setup_agent.py` 修复 + 文档）
- **执行角色**：子节点 1（Alibaba · DeepSeek V4.1 Flash 1017）
- **日期**：2026-11-22
- **状态**：实现与自测完成；未提交、未打 tag；待子节点 2 审查与 Phase 2 收口。

---

## 1. 交付摘要

| 交付物 | 路径 | 状态 |
| --- | --- | --- |
| 原生 Cordis 插件 | `plugins/dsh-jarvismind/` | 完成 |
| 真实 DSH 契约记录 | `plugins/dsh-jarvismind/CONTRACT.md` | 完成 |
| 插件接入文档 | `plugins/dsh-jarvismind/README.md` | 完成 |
| `setup_agent.py` 修复 | `examples/dsh-integration/python/setup_agent.py` | 完成 |
| `setup_agent.py` 回归测试 | `examples/dsh-integration/python/test_setup_agent.py` | 完成 |
| 接入教程更新 | `docs/tutorials/INTEGRATE-WITH-DSH.md`（第 8 节） | 完成 |
| 可安装 tarball | `/tmp/jarvis-pack/dsh-plugin-jarvismind-0.1.0.tgz` | 已生成（19 KB，19 文件） |

**未做**：独立只读审查、P0/P1 修复、原子提交、`v1.0.0` tag、真实 JarvisMind 跨会话验收（需安装后由用户/Phase 2 执行）。

---

## 2. 插件能力与真实 API 对应

| 需求 | 实现 | DSH 真实接口 |
| --- | --- | --- |
| 自动召回（Pre-chat） | 当前用户消息请求 `POST /api/v1/agent/context`，结果注入 system prompt | `agent/inbox/claimed` → `system-prompt/assemble` |
| 自动捕获（Post-chat） | 回合结束后异步 `POST /api/v1/agent/remember` | `session/event` → `turn/end` |
| 原生工具 | `jarvismind_search`、`jarvismind_remember`、`jarvismind_forget` | `ctx.tools.register(defineTool(...))` |
| 配置 Schema | `baseUrl`、`tokenFile`、`tokenEnv`、`autoRecall`、`recallLimit`、`autoRemember`、`timeoutMs` | schemastery `Config` |
| 生命周期 | 顶层会话自动接管，插件卸载可清理 | `agent/created` / `agent/disposed` / `agent.ctx.effect()` |

### 安全与稳定性

- **防注入**：`<jarvismind-context>` 附带只读/非指令声明；拒绝缺失、重复或畸形的 wrapper；`interpolate:false` 防止 `{{...}}` 被当作模板变量。
- **Fail-open**：默认 `timeoutMs=2500`；无 token、连接失败、超时、5xx 均静默跳过；工具错误作为普通工具结果返回，不结束回合。
- **零密钥泄露**：env 优先于文件，`~` 展开；日志/错误/tool 结果中的 `jmt_...` 统一脱敏；未读取或打印真实 `~/.jarvismind/dsh-agent.token`。
- **JMR 治理**：写入走 `/api/v1/agent/remember`；遗忘只创建两步 `PENDING` 申请，不删除、不绕过治理。
- **无本地记忆副本**：仅缓存当前用户消息的单次召回结果，`turn/end` 清除。
- **范围限制**：自动召回/捕获只附着于顶层会话；子 Agent 内部提示不自动入库。

---

## 3. 变更文件

### 新增

```
plugins/dsh-jarvismind/
  package.json
  package-lock.json
  tsconfig.json
  cordis.patch.yml
  icon.svg
  locale/en.json
  locale/zh.json
  README.md
  CONTRACT.md
  PHASE1-REPORT.md
  src/*.ts
  test/*.test.mjs
  test/stubs/*.mjs
  scripts/smoke-dsh.mjs
examples/dsh-integration/python/test_setup_agent.py
```

### 修改

```
examples/dsh-integration/python/setup_agent.py
docs/tutorials/INTEGRATE-WITH-DSH.md
```

`plugins/dsh-jarvismind/dist/` 与 `node_modules/` 由 `.gitignore` 排除；`.tgz` 不入库。

---

## 4. 验证证据

### 4.1 插件单元测试

```bash
cd plugins/dsh-jarvismind
npm test
```

结果：**35 / 35 通过**。覆盖：

- 配置默认值、边界钳制、`baseUrl` 凭证/查询串清理；
- token 环境变量优先、文件回退、`~` 展开、缺失降级；
- HTTP 请求头/请求体、搜索参数、remember/forget 端点；
- 超时 fail-open、5xx 错误码、无效 JSON、无 token；
- 召回注入、`interpolate:false`、畸形 wrapper 拒绝；
- 异步 remember 不阻塞、失败吞掉、非用户消息忽略；
- 令牌脱敏与日志不泄露；
- Cordis 生命周期接线（fake context + 本地 HTTP 服务）。

### 4.2 `setup_agent.py` 回归测试

```bash
cd examples/dsh-integration/python
python3 -m unittest -v test_setup_agent.py
```

结果：**3 / 3 通过**。

- 父目录不存在：自动创建 `0700`，token 文件 `0600`；
- 已存在父目录：保持原权限；
- 已存在宽权限 token 文件：写入后收紧为 `0600`。

### 4.3 真实 DSH 运行时 smoke

```bash
cd plugins/dsh-jarvismind
ELECTRON_RUN_AS_NODE=1 \
  "/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness" \
  --expose-internals scripts/smoke-dsh.mjs
```

结果：`SMOKE OK`。

验证了真实安装包（Cordis 4.0.4、schemastery 3.18.4、dsh-tools 0.2.0-rc.2）：

- 真实 `defineTool` 接受三个工具定义；
- 真实 `Config` 正确应用默认值；
- recall/capture 对本地 HTTP stub 完成请求；
- 日志中无测试令牌。

### 4.4 安装路径实测

- `dsh plugin --profile desktop add <本地目录>`：在一次性 profile 中成功，写入 `link:` 依赖并把 bundle 加入 `dsh.profile.bundles`。
- `dsh plugin --profile desktop add <.tgz>`：同样成功。
- `dsh --profile desktop plugin add ...`（launcher 形式）：被拒绝，报
  `error: profile "desktop" is managed exclusively by the Electron application`。
- 一次性 profile 已删除；真实 `~/.dsh/profiles/desktop/` 未被修改。

### 4.5 打包

```bash
npm pack --pack-destination /tmp
```

产物：`/tmp/jarvis-pack/dsh-plugin-jarvismind-0.1.0.tgz`（19 KB，19 文件，包含 `dist/`、`cordis.patch.yml`、`icon.svg`、`locale/`、`README.md`、`CONTRACT.md`、`scripts/smoke-dsh.mjs`）。

---

## 5. 安装与验收

### 方式 1：DSH Desktop 界面

DSH Desktop → **Plugins → Add plugin** → 填入绝对路径：

```
/Users/meikailong/JarvisMind/plugins/dsh-jarvismind
```

或 `.tgz` 路径。确认后启用 **JarvisMind Memory**。

### 方式 2：CLI（先 `⌘Q` 退出 DSH Desktop）

```bash
"/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" \
  plugin --profile desktop add /Users/meikailong/JarvisMind/plugins/dsh-jarvismind
```

### 2 分钟验收

1. 会话 A 告知持久偏好（如“我最喜欢的茶是大红袍”）。
2. 新建会话 B 提问验证自动召回。
3. 打开 `http://127.0.0.1:8777/console/memories` 查看/管理记忆。
4. 让模型遗忘：`jarvismind_forget` 只创建 `PENDING` 申请，在治理中心批准后才删除。

---

## 6. 风险与待办

| 项 | 说明 |
| --- | --- |
| 独立审查 | 子节点 2 的 P0/P1 只读审查尚未执行 |
| 原子提交 | 未提交、未打 tag（Phase 2 主 Agent 负责） |
| 真实跨会话验收 | 未对真实 JarvisMind 写入测试记忆；需安装后由用户执行 |
| 版本依赖 | 契约基于 DSH Desktop `0.2.0-rc.2` / Cordis `4.0.4`；升级后需复核 `CONTRACT.md` |
| UI 安装 | 按插件管理器文档与任务书提供；CLI 路径已实测 |

---

## 7. 结论

Phase 1 子节点 1 的插件实现、`setup_agent.py` 修复、回归测试和文档均已完成并通过自测；真实 DSH 运行时 smoke 通过；安装产物已生成。下一步进入子节点 2 只读审查与 Phase 2 收口。