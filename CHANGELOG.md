# Changelog

All notable changes to JarvisMind are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Data directory recommendation.** The READMEs, `docs/INSTALLATION.md` and `docs/deployment/MACOS.md` now tell a
  long-running install to put `JARVISMIND_DATA_DIR` outside the checkout (for example `~/.jarvismind/data`). Re-cloning
  or upgrading the repository otherwise takes `backend/data/` with it, and the empty database that follows looks exactly
  like "all my memories are gone".
- **Turn-end fallback for AI-decided capture (DSH plugin).** `rememberMode: agent` now keeps a safety net: when a
  turn ends and the model did not call `jarvismind_remember` during it, a substantive user message is submitted once
  with `mode: "auto"`, so the server's own intent and credential gates still judge it. Models forget the tool call —
  most often while summarising a long pasted document — and the user never sees that nothing was stored. The fallback
  is skipped when the model did remember, so a statement is never sent twice, and it never offers a pleasantry or a
  one-character reply. New option `fallbackCaptureOnTurnEnd` (default `true`).
- **Batch writes in `jarvismind_remember`.** Both the DSH plugin tool and the MCP tool accept `items: string[]`
  (maximum 20, enforced in code because the DSH tool schema DSL has no `maxItems`) alongside the existing `text`, which
  is unchanged and still supported. Each entry is its own governed `POST /api/v1/agent/remember`, so no schema change
  and no bypass: the credential gate, sensitivity hold and de-duplication run per statement. The result reports
  `saved` / `duplicate` / `protected` / `denied` / `failed` counts plus per-item rows, and a `PROTECTED` entry tells the
  model that the owner must approve it in the console. Duplicates inside one batch are collapsed.
- **Store-then-answer guidance for pasted material.** Both `memoryStyle` briefs now require the model to extract and
  store the durable facts from a self-introduction, an exported memory list, a chat-history export or a background
  profile **before** writing a summary — even when the user only asked for a summary. A summary in the chat is not a
  memory, and the pasted material does not survive compaction.
- **Embedding warm-up on `serve`.** Once the listener is bound, `serve` loads the embedding model in a daemon thread
  so the first recall does not pay for a cold ONNX load inside a request. `JARVISMIND_WARMUP_EMBEDDING` (default `1`,
  `0` to disable) controls it. It never delays startup, never runs under `pytest` or `TestClient` (only the `serve`
  command starts it), and fails open: no cache, no network, or a raising provider is logged once and dropped.
- **AI-decided memory and a companion style (DSH plugin).** `rememberMode` (`agent` by default, or `auto` / `both`)
  decides which mechanism selects memories: under `agent` the model calls `jarvismind_remember` in its own
  judgement instead of every turn being submitted, which stops greetings and one-line acknowledgements from being
  stored. `memoryStyle` (`assistant` by default, or `companion`) sets what the model is steered to notice — a
  companion brief adds shared references, significant dates, emotional turns and standing agreements, and asks for
  them to be recorded silently. The tool sends `mode: "direct"` so an AI-decided statement is written rather than
  refused for lacking a trigger word; the credential gate, sensitivity hold and de-duplication still run.
- **The knowledge graph works on a fresh install.** `python -m jarvismind init-db` and new-space provisioning now
  install `jkg_nodes`, `jkg_edges` and `jkg_edge_sources` and set `jkg.enabled` to `'1'` inside the database-creation
  transaction, so `GET /api/v1/jkg/status` reports `ready` and `/console/graph` works on first launch instead of
  reporting `jkg_schema_not_ready`. The installer is passed into `create_fresh_database` from the JKG package, so the
  database layer still does not import the graph, and `JARVISMIND_JKG_ENABLED=0` still overrides the stored default.
- **`mode` on the SDKs.** `remember()` in `sdk/python` and `sdk/typescript` accepts an optional `mode` and forwards
  it, without changing existing call signatures.
- **Soul continuity core anchor (DSH plugin).** The plugin now injects a `<jarvismind-core-anchor>` block on every
  step, ahead of the per-turn recall, holding the user's `PROFILE` memories — name, form of address, core
  preferences and standing agreements — so the relationship survives context compaction and new sessions. It
  reads `GET /api/v1/agent/memories`, ranks locally, truncates each entry, de-duplicates against the recall
  block, falls back to caching for 30 s in memory, and fails open. New options `coreAnchorEnabled`,
  `coreAnchorLimit` (1–12) and `coreAnchorMaxChars` (100–2000).
- **Native DSH Desktop plugin.** `plugins/dsh-jarvismind/` is a Cordis Host plugin for DSH Desktop 0.2.0-rc.2
  that automates the integration: recall before the reply via `POST /api/v1/agent/context`, asynchronous
  capture after `turn/end` via `POST /api/v1/agent/remember`, and the `jarvismind_search`,
  `jarvismind_remember` and `jarvismind_forget` tools. It keeps no memory of its own and fails open.
  See [the plugin README](plugins/dsh-jarvismind/README.md) and
  [section 8 of the DSH tutorial](docs/tutorials/INTEGRATE-WITH-DSH.md#8-native-dsh-desktop-plugin-automatic-hooks--tools).

### Fixed

- **The first recall no longer times out on a cold start.** The DSH plugin's `timeoutMs` default is now `6000`
  (clamp unchanged at 200–30000). The old `2500` was shorter than a first-time embedding load, so the first turn could
  silently drop its memory and look like data loss.
- **`setup_agent.py` on a fresh install.** The script creates missing parent directories for `--out` with
  mode `0700` and writes the token with mode `0600`, so `--out ~/.jarvismind/dsh-agent.token` no longer fails
  with `ENOENT` when `~/.jarvismind` does not exist yet.
- **A pleasantry is no longer stored as a memory.** With `mode: "direct"` there is no intent heuristic, so
  "谢谢" or "好的" could be written. A whole-message pleasantry and anything under two characters are now refused in
  both modes. The check is on the whole message, so "谢谢，我叫年糕" is still stored.
- **A `jmt_...` token is refused as memory content.** A statement containing a JarvisMind agent token was accepted in
  both modes; the credential gate did not recognise the token format the server itself issues.

## [1.0.0] - 2026-11-22

First release of the unified memory infrastructure.

### Added

- **Memory engine (JME).** Memory create, read, edit under governance, keyword and hybrid search, archive and restore,
  merge, a two-step forget, and a per-memory audit log. Six memory types and four statuses.
- **Governance (JMR).** Credential refusal before any write, personal-sensitivity holds for the owner, duplicate and
  conflict handling, an approval center for candidates, forget requests and modification requests, and non-human
  actor rules for agents.
- **Knowledge graph (JKG).** A derived graph of people, projects and events, with an installer, a database switch,
  initialization tasks, lifecycle sync, a bounded worker and a reader API.
- **Context engine (MCE).** Hybrid retrieval, a relevance floor, an untrusted-data context block and exclusion counts.
- **Memory spaces.** One database and one vector directory per additional space. Space selection fails closed.
- **Agents and tokens.** Agent identities with eight permissions, per-space grants, hashed tokens with expiry, rotation
  and revocation.
- **API v1.** The agent surface and the owner surface, one error envelope, sliding-window rate limits, a metadata-only
  access log, and a generated API reference.
- **Console.** Dashboard, memory explorer and detail, context lab, knowledge graph, governance center, memory spaces,
  agents, operations, API management, integrations, backups and exports, settings. Light and dark themes. Responsive
  layout.
- **Backups.** Per-space consistent backups with verification, and an offline restore script.
- **SDKs.** Python (`jarvismind-sdk`) and TypeScript (`@jarvismind/sdk`) agent clients.
- **DSH integration.** Pre-chat and post-chat hooks, an agent setup script, and a cross-session demo in Python and
  TypeScript.
- **Deployment files.** Dockerfile, compose file, container entrypoint, and CI workflows for tests, builds and releases.
- **Documentation.** Installation, configuration, deployment for Docker, Windows, Linux and macOS, architecture,
  memory management, governance, knowledge graph, agent integration, API, SDK, DSH tutorial, backup, security, security
  audit, troubleshooting, development guide, roadmap.

### Security

- Owner and agent credentials are checked on the server for every request. Agent tokens are stored only as HMAC digests
  and are shown once.
- Forget approval cannot be claimed by a requester. Agents cannot approve or correct memories.
- Sensitive text in content, summaries and metadata (keys, values and numbers) is withheld from agents and redacted in exports. Values under identifier-named keys are checked unless they have a system-generated shape.
- Access logs hold metadata only, with client addresses as keyed digests.
- Two read-only review rounds by separate reviewer agents were run, followed by a final self-check. Their findings and the fixes are listed in
  [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md), with the open items.

### Known limitations

- The release gate and the full test suite pass in the release environment. Docker, Windows and Linux installs were not run
  there. See the release report.
- Governance rules are configuration values, not editable objects.
- The knowledge graph cannot be edited by hand.
- Backups and exports are not encrypted.
- Rate limits are per process and keyed by the socket address.
- The sensitivity policy is a rule set. See the open items in the security audit.

### Provenance

The memory engine core was carried over from an earlier internal project in a clean-room copy. The release report describes
the copy and the checks made on it.

