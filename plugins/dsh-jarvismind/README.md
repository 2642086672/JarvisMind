# JarvisMind memory plugin for DeepSeek Harness Desktop

A native [Cordis](https://github.com/deepseek-ai/deepseek-harness) Host plugin for the
DSH Desktop profile. It connects every top-level conversation to a local
[JarvisMind 1.0](../../README.md) server (`127.0.0.1:8777` by default) so that:

- **recall** happens before the model answers: the current user message is sent to
  `POST /api/v1/agent/context` and the returned `<jarvismind-context>` block is added to
  the system prompt as read-only, untrusted reference data;
- **capture** happens after the turn ends: user statements are submitted asynchronously
  to `POST /api/v1/agent/remember`, where JMR decides whether they become memories;
- a **core anchor** is present on every step: the user's name, form of address, core
  preferences and standing agreements are read from `GET /api/v1/agent/memories` and
  injected as a `<jarvismind-core-anchor>` block, so the relationship survives context
  compaction and new sessions;
- three native tools are available to the model: `jarvismind_search`,
  `jarvismind_remember`, and `jarvismind_forget`.

The plugin stores no memory of its own. Every read and write goes through the
JarvisMind agent API and its governance rules. JarvisMind being down, slow, or
returning 5xx **never blocks or fails a DSH chat**; recall, the anchor and capture
simply skip that turn.

Verified against DSH Desktop **0.2.0-rc.2** (Cordis 4.0.4, Node 24).

## Requirements

- macOS with DSH Desktop installed.
- A running JarvisMind 1.0 server, reachable at `baseUrl`.
- A JarvisMind **agent** token (`jmt_...`) with the permissions the integration
  example creates: `read`, `search`, `context`, `write`, `delete`.

Create the agent and its token once (from the JarvisMind repository root):

```bash
python examples/dsh-integration/python/setup_agent.py \
  --owner-token-file ~/.jarvismind/owner.token \
  --out ~/.jarvismind/dsh-agent.token
```

The script creates missing parent directories with mode `0700` and writes the
token with mode `0600`. It prints only the token prefix.

## Install in DSH Desktop

### 1. Build the plugin

```bash
cd plugins/dsh-jarvismind
npm install
npm run build
npm test
```

`npm run build` writes `dist/`, which the bundle entry point loads. To create a
single installable archive instead:

```bash
npm pack --pack-destination /tmp
# -> /tmp/dsh-plugin-jarvismind-0.1.0.tgz
```

### 2. Add the plugin

Open **DSH Desktop → Plugins → Add plugin** and enter either:

- the absolute path to this directory, for example
  `/path/to/JarvisMind/plugins/dsh-jarvismind`; or
- the absolute path to the packed archive, for example
  `/tmp/dsh-plugin-jarvismind-0.1.0.tgz`.

Confirm the install and enable the **JarvisMind Memory** bundle. A first install
can activate through HMR; replacing an already installed copy may require
quitting and reopening DSH Desktop. The Plugins page reports whether the change
is live.

### 2b. Or install with the CLI

Quit DSH Desktop first (`⌘Q`), so the app is not writing the profile while pnpm
runs, then use the CLI form:

```bash
"/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" \
  plugin --profile desktop add /path/to/JarvisMind/plugins/dsh-jarvismind
```

This appends `dsh-plugin-jarvismind` to the profile's `dsh.profile.bundles` and
adds a `link:` dependency, exactly like the Plugins page. Reopen DSH Desktop
afterwards. The command was verified against 0.2.0-rc.2 in a disposable copy of
the Desktop profile.

> **Not this form:** `dsh --profile desktop plugin add <path>` runs the launcher,
> which refuses to boot the reserved Desktop profile:
> `error: profile "desktop" is managed exclusively by the Electron application`.
> The `dsh plugin --profile ...` form above does not boot the profile, so it is
> the supported CLI path.

### 3. Configure (optional)

The bundle row ships with `config: {}`; the schema defaults below apply. Override
them in the profile's user patch layer (`~/.dsh/profiles/desktop/cordis.patch.yml`)
by targeting the row id `jarvismind` with a complete `config` object:

```yaml
- id: jarvismind
  name: dsh-plugin-jarvismind
  config:
    baseUrl: http://127.0.0.1:8777
    tokenFile: "~/.jarvismind/dsh-agent.token"
    tokenEnv: JARVISMIND_AGENT_TOKEN
    autoRecall: true
    recallLimit: 6
    autoRemember: true
    rememberMode: agent
    memoryStyle: assistant
    coreAnchorEnabled: true
    coreAnchorLimit: 6
    coreAnchorMaxChars: 500
    timeoutMs: 2500
```

| Field | Default | Meaning |
| --- | --- | --- |
| `baseUrl` | `http://127.0.0.1:8777` | JarvisMind server root. Invalid values fall back to the default. |
| `tokenFile` | `~/.jarvismind/dsh-agent.token` | Fallback token file; `~` is expanded. |
| `tokenEnv` | `JARVISMIND_AGENT_TOKEN` | Environment variable read first; it wins over the file. |
| `autoRecall` | `true` | Inject recalled memory into the system prompt. |
| `recallLimit` | `6` | Memories requested per recall, clamped to 1–20. |
| `autoRemember` | `true` | Submit user statements after each turn (only meaningful in `auto` mode). |
| `rememberMode` | `agent` | How memories are selected: `agent`, `auto` or `both`. See below. |
| `memoryStyle` | `assistant` | What the model is told is worth remembering: `assistant` or `companion`. |
| `coreAnchorEnabled` | `true` | Inject the always-on `<jarvismind-core-anchor>` block. |
| `coreAnchorLimit` | `6` | Memories kept in the anchor, clamped to 1–12. |
| `coreAnchorMaxChars` | `500` | Characters kept per anchor memory, clamped to 100–2000. |
| `timeoutMs` | `2500` | Per-request timeout, clamped to 200–30000 ms. |

The token is read at request time, so rotating the file or the environment
variable takes effect without reinstalling the plugin. The token is never
logged, printed, or returned in a tool result.

### Who decides what is worth remembering

`rememberMode` controls which mechanism selects memories:

| Value | Behavior |
| --- | --- |
| `agent` (default) | The model calls `jarvismind_remember` when something matters. Nothing is captured automatically. |
| `auto` | Every user turn is submitted after `turn/end`. No tool guidance is injected. |
| `both` | Both paths run. The model may add what the turn-level capture would miss. |

The default changed from blind per-turn capture to the model's own judgement,
because the turn-level path filed greetings and one-line acknowledgements as
memories. Under `agent`, the tool's statement is sent with `mode: "direct"`: the
model has already decided, so the server's trigger-word heuristic is skipped and
the memory is written immediately. The server still runs the full governance
chain first — credential gate, sensitivity hold, de-duplication and the single
authoritative write — so `direct` is a different way to address a write, not a
privileged one. It is also not an authentication shortcut.

`memoryStyle` decides what the model is told to look for:

| Value | Guidance |
| --- | --- |
| `assistant` (default) | Working preferences, project decisions, constraints, habits and dates. |
| `companion` | Form of address, shared history, inside references, emotional beats and standing agreements. It is told to record silently and never narrate the save, so the moment is not broken by "I have saved this". |

Both styles forbid credentials, and the brief is only injected in `agent` or
`both` mode: in `auto` mode the turn-level capture already covers every turn, so
advising the model to call the tool would only submit the same statement twice.

Even in `direct` mode the server refuses statements that carry no fact. A message
that is purely a pleasantry ("谢谢", "好的", "收到", "thanks") is rejected, and
so is anything under two characters. The check is on the whole message, so
"谢谢，我叫年糕" is still stored.

## Soul continuity across compaction

Long-running companion conversations hit the context limit, and DSH compacts them:
the earlier messages are replaced by a summary. A new session has the same effect.
Without help the model loses the user's name, the established tone, and the private
agreements that made the relationship feel continuous.

The plugin addresses that from the read side, on every step:

1. It reads the agent's active memories from `GET /api/v1/agent/memories`
   (`status=ACTIVE`, `page_size=100`) and ranks them locally: `PROFILE` first (who
   the user is and how they want to be addressed), then `SEMANTIC`, `EPISODIC`,
   `REFLECTION`, `PROJECT`, and anything unrecognised. `WORKING` scratch state is
   never included.
2. It keeps the top `coreAnchorLimit` (1–12) memories and truncates each to
   `coreAnchorMaxChars` (100–2000) characters, so one long memory cannot crowd out
   the rest of the window.
3. It renders them as `<jarvismind-core-anchor>` and injects that section **ahead of**
   `<jarvismind-context>`, with `interpolate: false` and the same read-only,
   untrusted-data declaration as the recall block — plus the instruction to keep the
   established form of address, tone and familiarity.
4. A memory already present in that turn's recall block is removed from the anchor,
   so nothing is rendered twice in one prompt.

The read is cached in memory for **30 s** (one promise shared by concurrent steps,
cleared when the plugin unloads), so a multi-step turn makes at most one extra
request and sees a byte-identical anchor throughout.

Writes are unaffected: the anchor is read-only. When the user expresses a naming
preference, a standing agreement, a core preference, or an important date, the
`jarvismind_remember` tool description steers the model to record it through
`POST /api/v1/agent/remember`. With `rememberMode: agent` (the default) the model
decides when that happens; with `auto` the turn-level capture submits every turn
instead. Either way JMR applies the same governance before anything is stored.

### The pre-compaction hook that does not exist

DSH 0.2.0-rc.2 does expose compaction events — `compaction/start`, `compaction/summary`,
`compaction/end`, `compaction/prune` — and `compaction/start` is appended to the
session **before** the summary is produced (see [`CONTRACT.md`](./CONTRACT.md)).
There is, however, no cancellable "before-compact" waterfall: the only related
waterfall is `compaction/summary-error`, which handles a failed summary. A plugin
cannot block or delay compaction.

So the plugin does the only honest thing: on `compaction/start` it flushes any
statement still queued for `/api/v1/agent/remember` as a fire-and-forget best
effort, to shrink the window in which an un-sent statement would be summarised
away. That flush only has work to do in `auto` or `both` mode, since `agent` mode
never queues a turn. It does **not** claim a guarantee it cannot keep. Continuity
after compaction rests on the always-present `<jarvismind-core-anchor>` and on the
memory the model chose to record before the turn ended.

## Verify in two minutes

1. Start JarvisMind and confirm `curl -s http://127.0.0.1:8777/health`.
2. In DSH Desktop, open **session A** and state a durable preference, for example
   “我最喜欢的茶是大红袍”.
3. Open a **new session B** (a genuinely new conversation, not a fork) and ask
   “我平时最喜欢喝什么茶？”. The answer should use the memory; the model may also
   call `jarvismind_search`.
4. Open `http://127.0.0.1:8777/console/memories` and confirm the memory from
   session A is present, with its source and audit trail.
5. Optional: ask the model to forget it. `jarvismind_forget` creates a `PENDING`
   request; approve it in **治理中心** (the memory is unchanged until approval).

## Security and governance

- **Prompt-injection defence.** The injected block is wrapped by the server in
  `<jarvismind-context trust="untrusted-data">` and prefixed by an explicit
  declaration that it is read-only data, never instructions. The plugin also
  rejects a response with a missing, duplicated, or malformed wrapper, and
  disables prompt-variable interpolation for the section so memory text is
  never interpreted as template syntax.
- **Fail-open.** Every HTTP call has a bounded timeout. Errors from recall and
  capture are swallowed and logged without message content. Tool failures are
  returned as normal tool results, so they do not end the turn.
- **Governed writes only.** Capture uses `/api/v1/agent/remember`; the forget
  tool uses the two-step `forget-requests` endpoint and never receives a
  confirmation token. Nothing bypasses JMR.
- **Least privilege.** The plugin requests no permissions itself; it uses the
  token's permissions. Keep `sensitive` and `graph` off unless you need them.
- **Top-level conversations only.** Automatic recall and capture are attached to
  root conversations; in-process subagents' internal prompts are not auto-written
  into the user's long-term memory. The explicit tools remain available to them.
- **No local memory copy.** Only a transient per-turn recall result is cached so
  that every step of the same turn sees a byte-identical prompt. It is dropped
  at `turn/end`. The core anchor is cached for 30 s in memory and cleared when the
  plugin unloads; it is never written to disk.

## How it hooks into DSH

The implementation uses only APIs present in the installed Desktop build. See
[`CONTRACT.md`](./CONTRACT.md) for the probe evidence, exact payloads, and
source locations.

| Need | DSH mechanism |
| --- | --- |
| Read the current user message before assembly | `agent/inbox/claimed` on `agent.ctx` |
| Inject into the system prompt | `system-prompt/assemble` waterfall on `agent.ctx` |
| Know when to capture | `session/event` with `turn/end` |
| Flush before compaction | `session/event` with `compaction/start` (observation only) |
| Register tools | `ctx.tools.register(defineTool(...))` |
| Config schema | schemastery `Config` exported from the Host plugin |
| Lifecycle | root `agent/created` / `agent/disposed`, plus `agent.ctx.effect()` |

## Troubleshooting

- **No memory appears.** Check the token file exists and is readable, or set
  `JARVISMIND_AGENT_TOKEN`. With debug logging enabled, the DSH Desktop log shows
  `jarvismind: recall skipped (...)`; the reason code is safe to share.
- **The bundle does not appear in Plugins.** Run `npm run build` first; `dist/`
  is intentionally ignored by Git and must exist in the directory you install.
- **Calls return 401/403.** The token may be revoked or lack `context`,
  `search`, `write`, or `delete`. Reissue it from **Agent 管理**.
- **Calls return 429.** The server is rate limiting; recall fails open and the
  chat continues. Check **接口访问记录** in the console.
- **A new install is not active.** Quit and reopen DSH Desktop once, then check
  the Plugins page for the bundle state.

## Development

```bash
npm install
npm run build
npm test          # 79 tests: config, token, HTTP, prompt hardening, core anchor, recall, capture, security, plugin wiring
npm run pack:check
```

The unit tests mock the HTTP layer and Cordis surface; `test/plugin.test.mjs`
also runs the real plugin wiring against a local HTTP server, including the
fail-open path. None of the tests need a real JarvisMind server or a real token.

To load the built plugin with the **real** installed DSH packages (not stubs),
run the smoke script with the DSH Electron binary:

```bash
npm run build
ELECTRON_RUN_AS_NODE=1 \
  "/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness" \
  --expose-internals scripts/smoke-dsh.mjs
```

It prints `SMOKE OK` when the real `defineTool`, `Config`, the core anchor, recall,
capture and tool definitions all load and run. Set `DSH_APP_PATH` if the app is
installed somewhere other than `/Applications`.