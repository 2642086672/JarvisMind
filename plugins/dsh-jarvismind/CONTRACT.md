# DSH Desktop Cordis contract used by this plugin

This file records the read-only probe of the installed DSH Desktop build. The
plugin is written against these exact names and payloads; none of them are
invented. Evidence paths are inside
`/Applications/DeepSeek Harness.app/Contents/Resources/app.asar`.

## Versions

| Component | Value | Evidence |
| --- | --- | --- |
| DSH Desktop | `0.2.0-rc.2` | `runtime/primary-runtime/runtime.json` |
| Cordis | `4.0.4` | `dsh/node_modules/@deepseek-ai/cordis/package.json` |
| Runtime Node | `24.18.1` (CLI), `24.21.0` (primary runtime) | `runtime/versions.json`, `runtime.json` |

## Host plugin shape

From `dsh/node_modules/@deepseek-ai/dsh-agent-preset/skills/cordis-plugin-development/references/host-plugin.md`:

- A Host plugin exports one of:
  - `export function apply(ctx, config)`, optionally `export const inject = [...]` and `export const Config`;
  - or a service class as the default export.
- A bundle is a package whose `package.json` declares
  `"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }`. The patch inserts
  rows; each row has `id`, `name`, and optional `config`.
- A plugin can resolve packages shipped with DSH without declaring them as
  dependencies (`dsh-app-boot/lib/index.js`: profile `node_modules` first, then
  the installation's runtime resolver).
- `Config` is a schemastery schema. The installed `@deepseek-ai/schemastery`
  is `3.18.4` (`dsh/node_modules/@deepseek-ai/schemastery/package.json`).

## APIs used

### Prompt assembly

`dsh-system-prompt/lib/index.js`:

- `SystemPrompt.assemble(context)` calls
  `ctx.waterfall(scopeTarget(this, scope), 'system-prompt/assemble', assembly, context, () => Promise.resolve(assembly))`.
- A listener receives `(assembly, context, next)`; `next()` returns the final
  assembly. `assembly.sections` is the ordered prompt text; `interpolate: false`
  preserves literal `{{...}}` in memory text.
- `dsh-agent/lib/types/dispatch.js` `assembleContextFor(agent, signal)` returns
  `{ agent, scope: agent, signal }`.
- `dsh-agent-loop/lib/index.js` `preStep()` claims the inbox batch, then calls
  `systemPrompt.assemble(assembleContextFor(this, signal))`. The assembly runs
  **before** `agent/pre-step`, which is why this plugin recalls from the inbox
  claim rather than from `agent/pre-step`.

### Agent lifecycle and inbox

`dsh-agent/lib/index.js`:

- `agent/created` is a serial event with `{ agent, source, signal }`.
- `agent/disposed` has `{ agent }`.
- `dsh-agent-loop/lib/index.js` `ReactLoopInbox.claim()` emits
  `agent/inbox/claimed` with `{ message, turn }` synchronously, before returning
  the claimed batch.

`dsh-agent/lib/types/dispatch.js` `agentEvents()` dispatches through
`scopeTarget(agent, agent)`, so a listener registered on `agent.ctx` receives
these agent-scoped events.

### Turn end

`dsh-session/lib/index.js` appends `turn/end` with
`{ turn, reason: { kind } }`; `session/event` listeners receive
`(session, event)`.

### Context compaction

Probed in `0.2.0-rc.2`. The session event map declares four compaction events:
`compaction/start` (`dsh-agent-loop`), `compaction/summary`, `compaction/end`, and
`compaction/prune`.

- `Session.append()` (`dsh-session/lib/index.js`, the `append` method) publishes
  **every** appended event on `session/event` — the publish call is unconditional
  on the event type — so a `ctx.on('session/event', ...)` listener receives
  compaction events exactly like `turn/end`. Built-in plugins rely on this: the
  analytics plugin filters `session/event` for `compaction/start`.
- `compaction/start` is appended **before** the summary is produced, and it is a
  durable marker (it survives replay).

**There is no cancellable before-compaction hook.** The only compaction-related
waterfall is `compaction/summary-error`, which runs when producing a summary
throws. A listener therefore cannot veto, delay, or await compaction.

Consequence for this plugin: `compaction/start` is an observation point only. It
is used to flush queued capture statements as a fire-and-forget best effort, and
the always-present `<jarvismind-core-anchor>` is what actually preserves
continuity across a compaction. No blocking behavior is claimed.

### Agent memory list

The core anchor reads the user's defining memories rather than a query match, so
it uses the same documented agent surface as the tools:

- `GET /api/v1/agent/memories?status=ACTIVE&page=1&page_size=<n>` returns the
  agent's readable active memories. `page_size` is capped at 100 by
  `MemoryListQuery`, so the anchor reads one page and ranks it locally.
- The endpoint returns `MemoryResponse` records with `id`, `content`,
  `memory_type`, and `importance`; those are the fields the anchor ranks on.
- The server flattens memory text (`_flatten`) and escapes markup
  (`_escape_markup`) inside `context_block()`. The core anchor is rendered by the
  plugin instead, so it applies the same flatten-and-escape rules locally.
- Ranking happens in the plugin because the endpoint cannot sort by importance:
  `PROFILE` first, then `SEMANTIC`, `EPISODIC`, `REFLECTION`, `PROJECT`, then any
  unrecognised type, and never `WORKING`.

### Tools

`dsh-tools/lib/index.js`:

- `defineTool({ name, description, parameters, output, execute })` validates the
  parameter schema, output schema, and arguments.
- `ToolRuntime.register(definition)` registers globally or per-agent; schemas
  flow into prompt assembly automatically.
- Tool execution context exposes `exec.signal` and `exec.agent` (the live Agent,
  including `exec.agent.session.id`).
- A tool description is part of the prompt, not just UI text: the wording of
  `jarvismind_remember` is what steers the model to record a form of address, a
  standing agreement or a shared reference. That description, plus the
  `memoryStyle` brief injected by this plugin, is the entire mechanism behind
  `rememberMode: agent`.

### Writing through the agent API

`POST /api/v1/agent/remember` accepts an optional `mode`:

- `auto` (the server default when `mode` is omitted) applies the server's own
  intent heuristics, so a statement that does not read like a memory request is
  refused with `NO_INTENT`.
- `direct` skips those heuristics. The plugin sends it from the
  `jarvismind_remember` tool because the model has already made the decision.

`direct` changes *which* heuristic runs first, never the governance chain. The
credential gate, the JMR sensitivity hold, de-duplication and the single
authoritative write all still run; a credential is still refused, a sensitive
personal fact still waits for approval, and a duplicate is still dropped.
Nothing about authentication changes either.

Because `direct` does not judge whether a statement is worth keeping, the plugin
only sends what the model chose to send, and the server still refuses a message
that carries no fact at all: a whole-message pleasantry (`谢谢`, `好的`, `收到`,
`thanks`) or anything under two characters is rejected. The check is on the
whole message, so `谢谢，我叫年糕` is stored.

### Scoping and cleanup

- `agent.ctx` is created by `createScope(loopCtx, agent)` in
  `dsh-scope/lib/index.js`; its scope key is the agent object.
- `agent.ctx.effect()` / `agent.ctx.on()` return disposers tied to the agent.
  The plugin also keeps the disposer in its own effect so plugin unload removes
  the agent registrations.

## Install behavior (verified)

- `dsh plugin --profile desktop add <absolute path>` works: it runs pnpm against
  the profile, adds a `link:` dependency to `dsh.profile.bundles`, and is the
  scriptable install path. Verified in a disposable copy of the Desktop profile.
- `dsh plugin --profile desktop add <.tgz>` works the same way with a `file:`
  dependency.
- `dsh --profile desktop ...` (launcher form) is rejected by
  `dsh/lib/bin.js`: `error: profile "desktop" is managed exclusively by the
  Electron application`.
- The Desktop **Plugins → Add plugin** page accepts the same local path or
  tarball through the plugin manager's `install_bundle` path.

## Runtime smoke test

`plugins/dsh-jarvismind` was loaded with the real installed packages (Cordis,
schemastery, dsh-tools) under the DSH runtime Node with `ELECTRON_RUN_AS_NODE=1`.
The real `defineTool` accepted all three definitions, the real `Config` applied
its defaults (including the three core-anchor keys), and the full
anchor/recall/capture flow ran against a local HTTP stub.