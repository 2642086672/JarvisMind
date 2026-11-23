# JarvisMind 1.0

**English** | [简体中文](README.zh-CN.md)

> 我要给她完整的一生。
> *I will give her a whole life.* — from *The Wandering Earth* (《流浪地球》)

JarvisMind is an open, self-hosted **unified memory infrastructure for AI systems**. It gives any
assistant (a chat app, an agent, a desktop tool, an enterprise system) one place to remember,
recall, govern and forget what a user has told it, and one web console where the owner can see and
correct all of it.

- **Authoritative memory (JME).** Each memory space keeps its memories in one SQLite database. No other
  component keeps a copy that can drift.
- **Governance (JMR).** Credentials are refused before any write. Sensitive personal facts wait for the
  owner. Agents cannot approve their own requests or correct memories themselves.
- **Knowledge graph (JKG).** A view of people, projects, events and relations derived from memories.
  It is never a second memory store.
- **Context engine (MCE).** Finds relevant memories and builds a context block. The block is marked as
  untrusted data, so a model reads it as reference material, not as instructions.
- **Console.** The owner's browser console: memories, graph, governance, spaces, agents, operations,
  API management, backups, settings.
- **API v1 and SDKs.** A versioned `/api/v1` surface for agents, with Python and TypeScript clients.
- **DSH example and native plugin.** A runnable integration with a cross-session demo, plus a Cordis
  plugin (`plugins/dsh-jarvismind/`) that wires recall and capture into DSH Desktop automatically. The
  plugin keeps a `<jarvismind-core-anchor>` block in every system prompt, so a form of address, a standing
  agreement or a shared reference survives context compaction and a new session. It also lets the model —
  rather than a per-turn trigger — decide what is worth remembering, and offers a `companion` style for
  assistants kept as long-term companions.

## What's new

- **The model decides what to remember.** `rememberMode: agent` is now the plugin default: instead of
  submitting every turn, the model calls `jarvismind_remember` when something matters. A greeting or a
  one-word acknowledgement no longer becomes a memory candidate.
- **A `companion` style.** Besides working preferences and decisions, the model can be steered to notice
  shared references, significant dates, emotional turns and standing agreements — and to record them
  silently, without announcing the save.
- **Soul continuity across compaction.** The `<jarvismind-core-anchor>` block is rebuilt on every turn from
  your core profile, form of address and long-term relationship memories, and de-duplicated against the
  per-turn recall. It is data, never a template and never an instruction.
- **The knowledge graph works on a fresh install.** `init-db` and new-space provisioning create the graph
  tables inside the database-creation transaction, so the console's graph view is not empty on first launch.
- **Core anchor keeps older profile memories.** PROFILE memories are now read with their own query before any
  other type, so an old name or form of address no longer drops out when many newer memories exist. The anchor
  reads up to the newest 100 PROFILE memories; beyond that the oldest are still not included.
- **Windows fixes.** The console no longer answers a missing asset with `index.html` on Windows paths, `~\`
  token paths expand correctly, and the setup-agent tests check permissions only where POSIX modes exist.
- **Test dependencies are separate.** `requirements.txt` holds the runtime dependencies only;
  `requirements-dev.txt` adds pytest for running the suite.

## Architecture

```text
   Owner (browser)                          Agents and apps (DSH, DeepSeek, Claude, custom ...)
   owner credential                         agent token (jmt_...), per-space grants
        |                                                     |
        v                                                     v
  /api/v1/console, /api/v1/memories, ...               /api/v1/agent/*
        |                                                     |
        +------------------------+----------------------------+
                                 v
              JarvisMind API (FastAPI, one process)
              +----------------------------------------------------------+
              |  MCE  context and recall   --reads-->  JME  memories     |
              |  JMR  governance gate     --guards-->  JME  writes/audit |
              |  JKG  graph (derived)     <--feed---   JME  audit log    |
              +----------------------------------------------------------+
                                 |
        control.db       : spaces, agents, grants, token digests, access log (metadata only)
        jarvismind.db    : default memory space (JME + JMR + JKG tables)
        spaces/<id>/     : one physical store per additional space (database + vectors)
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the principles, the write and forget paths, and
the isolation model.

## Quick start

Requirements: Python 3.13 (the version used for the release tests). Node.js 20 or newer is needed only to
build the console.

```bash
cd backend
python3.13 -m venv .venv
. .venv/bin/activate              # Windows: see docs/deployment/WINDOWS.md
pip install -r requirements.txt
python -m jarvismind init-db      # creates ./data/jarvismind.db and ./data/control.db
python -m jarvismind serve        # http://127.0.0.1:8777
```

To run the test suite, install the development requirements and run pytest from `backend/`:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Check it:

```bash
curl -s http://127.0.0.1:8777/health
```

Build the console and serve it at `/console/`:

```bash
(cd ../console && npm ci && npm run build)
JARVISMIND_CONSOLE_DIR="$(cd ../console && pwd)/dist" python -m jarvismind serve
```

The owner token is in `backend/data/.jarvismind_token` (created on first start, mode 0600), or in the
`JARVISMIND_API_TOKEN` environment variable if you set one. Sign in at `http://127.0.0.1:8777/console/`.

Step-by-step guides: [Installation](docs/INSTALLATION.md), [Configuration](docs/CONFIGURATION.md),
[Docker](docs/DOCKER.md), [Windows](docs/deployment/WINDOWS.md), [Linux](docs/deployment/LINUX.md),
[macOS](docs/deployment/MACOS.md).

## Integrate an agent

1. Create the agent and its token in the console (**Agent 管理**), or with
   `examples/dsh-integration/python/setup_agent.py`.
2. Before a reply, call `POST /api/v1/agent/context` and put the returned block in the system prompt.
3. After a turn, call `POST /api/v1/agent/remember`. JMR decides what is stored.
4. Forgetting is two-step and the owner decides it. See [docs/AGENT-INTEGRATION.md](docs/AGENT-INTEGRATION.md).

SDKs: [Python](sdk/python/README.md) and [TypeScript](sdk/typescript/README.md). The full DSH walk-through is
in [docs/tutorials/INTEGRATE-WITH-DSH.md](docs/tutorials/INTEGRATE-WITH-DSH.md). For DSH Desktop the same
hooks are available as a native plugin: [plugins/dsh-jarvismind](plugins/dsh-jarvismind/README.md).

A fresh `init-db` also ships the [knowledge graph](docs/KNOWLEDGE-GRAPH.md) ready to use, so the console's graph
view works on first launch.

## What this release has been verified against

| Area | Result in the release environment |
| --- | --- |
| Backend test suite (including the P0 release gate) | 2533 passed, 0 failed, 0 skipped; gate 67/67 |
| Console | typecheck clean, 15 unit tests pass, production build succeeds |
| Python SDK | 10 tests pass |
| TypeScript SDK | 9 tests pass |
| DSH plugin | 79 tests pass; build clean; `SMOKE OK` against DSH Desktop 0.2.0-rc.2 |
| DSH cross-session demo | 16/16 checks, Python and TypeScript, against a live server |
| Windows (local development machine) | Full backend suite, console build and a browser smoke run (login, dashboard, graph `ready`, theme switch, no console errors). A fresh offline machine can take about 40 s on the first memory list while the embedding model download retries |
| Docker image, Linux install, a real DSH application | **not verified** (see [the release report](RELEASE-1.0-FINAL-REPORT.md)) |

Limits and open items are listed in [docs/ROADMAP.md](docs/ROADMAP.md) and in the release report.

## Documentation

All documentation is indexed in [docs/README.md](docs/README.md). Security status is in
[docs/SECURITY.md](docs/SECURITY.md) and [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md).

## Contributing, license

- [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [SECURITY.md](SECURITY.md)
- [CHANGELOG.md](CHANGELOG.md) (versioning follows SemVer)
- Licensed under the MIT License, see [LICENSE](LICENSE).
