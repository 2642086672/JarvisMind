# JarvisMind 1.0

[简体中文](README.md) | **English**

> *I will give her a whole life.* —— from *The Wandering Earth*

**JarvisMind** is an open, self-hostable **unified memory infrastructure for AI systems** (MIT licensed). It lets any AI assistant — **DeepSeek Harness (DSH)**, Claude, ChatGPT, a local agent, or an enterprise system — remember, recall, govern and truly forget what the user has told it, all in one place. A visual web console lets the owner view, correct and control every memory at any time.

## 📣 Feedback & community

Every piece of feedback matters during the public beta:

* 🐛 **Report a bug** · 💡 **Request a feature** · 🧪 **Share beta impressions**: [pick a feedback type here](https://github.com/2642086672/JarvisMind/issues/new/choose) (takes ~30 seconds; blank issues are welcome too)
* Stuck on installation, confused by the console, or the companion mode feels off? Just tell us — no formal write-up needed.
* ⚠️ Never paste real API tokens or personal keys; report security issues privately via [SECURITY.md](SECURITY.md).

## Why JarvisMind?

* **No more black-box amnesia:** different AI tools share one self-hosted memory, so switching clients no longer means starting from zero.
* **No losing the persona during context compaction:** when the native context fills up or a new session starts, a resident `<jarvismind-core-anchor>` rebuilds the form of address, the established rapport and the long-term agreements on every turn, so a companion AI does not suddenly revert to a stranger-like support bot.
* **Safe, auditable and truly forgettable:** API keys and passwords are blocked before they are stored; sensitive personal facts require the owner's approval; forgetting is a two-step approval, and once executed the memory disappears completely from the database, the vector store and the graph view.

## Core capabilities

* **Authoritative memory engine (JME):** each memory space has its own SQLite database, physically isolated. No drifting multi-copy stores are kept.
* **Pre-write governance gate (JMR):** credentials and secrets are blocked automatically; sensitive personal facts go to a pending-approval queue; agents cannot approve their own requests or change memories beyond their permissions.
* **Derived knowledge graph (JKG, ready out of the box):** people, projects, events and relations are derived automatically from the memory audit stream. A fresh `init-db` and newly created spaces create the graph tables by default, so the graph is visible in the console on first launch.
* **Context recall engine (MCE):** hybrid keyword and vector retrieval. It assembles context within a budget and explicitly marks it as untrusted data (`trust="untrusted-data"`) to resist prompt injection.
* **Unified web console:** manage memories, the knowledge graph, governance approvals, multiple memory spaces, agent tokens and permissions, operations, the API and backups from one browser UI.
* **API v1, SDKs in two languages and a native DSH plugin:** a versioned `/api/v1/agent/*` interface, a zero-runtime-dependency [Python SDK](sdk/python/README.md) and a [TypeScript SDK](sdk/typescript/README.md), and a native Cordis plugin for **DeepSeek Harness Desktop** ([plugins/dsh-jarvismind](plugins/dsh-jarvismind/README.md)).

## Recent highlights

* **The model decides what is worth remembering (`rememberMode: agent`):** instead of blindly writing every "okay / thanks" to memory, the active AI distils the information with lasting value and calls `jarvismind_remember`. The backend stores it directly through `mode: direct`, with credential blocking, sensitivity governance and de-duplication all still applied.
* **`companion` memory style:** besides the everyday work style (`assistant`), the `companion` style guides the AI to notice shared memories, inside references, important dates, emotional moments and standing agreements, and to record them **silently, without ever announcing "saved"**, to keep the interaction natural.
* **The core anchor is no longer limited to the newest 100 memories:** `<jarvismind-core-anchor>` first queries `PROFILE` memories on their own, then fills the remaining room with other types. It reads at most the newest 100 `PROFILE` memories, so early names and core agreements are not pushed out by later memories.
* **The knowledge graph is ready out of the box:** `init-db` and new spaces create the tables inside the database-creation transaction and enable `jkg.enabled`, so the graph reports `ready` on first start.
* **Windows verification and split dependencies:** fixes for the Windows console static-asset handling and `~\` token paths; test dependencies moved into `backend/requirements-dev.txt`.

## Architecture

```text
   Owner (browser console)                  Agents and apps (DSH, DeepSeek, Claude, custom ...)
   owner credential                         agent token (jmt_...), least-privilege per space
        |                                                       |
        v                                                       v
  /api/v1/console, /api/v1/memories, ...                 /api/v1/agent/*
        |                                                       |
        +--------------------------+----------------------------+
                                   v
                JarvisMind API (FastAPI, single process)
                +----------------------------------------------------------+
                |  MCE  context and recall   --reads-->  JME  single       |
                |                                        authoritative    |
                |  JMR  governance gate      --guards--> JME  writes/audit |
                |  JKG  graph (derived)      <--audit--  JME  audit log    |
                +----------------------------------------------------------+
                                   |
        control.db       : spaces, agents, grants, HMAC token digests, access log (metadata only)
        jarvismind.db    : default memory space (JME + JMR + JKG tables, schema v6)
        spaces/<id>/     : one physically isolated store per additional space (SQLite + vectors)
```

For the principles, the write and two-step forget paths, and the isolation model, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 3-minute quick start

Requirements: **Python 3.13** (the version used for the release tests); **Node.js 20+** to build the web console.

### 1. Clone and build the web console

```bash
git clone https://github.com/2642086672/JarvisMind.git
cd JarvisMind
(cd console && npm ci && npm run build)
```

### 2. Initialise and start the service

**macOS / Linux:**

```bash
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m jarvismind init-db
JARVISMIND_CONSOLE_DIR="$(cd ../console && pwd)/dist" python -m jarvismind serve
```

**Windows (PowerShell):**

```powershell
cd backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m jarvismind init-db
$env:JARVISMIND_CONSOLE_DIR = (Resolve-Path ..\console\dist).Path
python -m jarvismind serve
```

### 3. Verify, then sign in to the console

```bash
curl -s http://127.0.0.1:8777/health
```

On first start the owner token is generated at `backend/data/.jarvismind_token` (mode `0600`). You can also set it with the `JARVISMIND_API_TOKEN` environment variable. Open `http://127.0.0.1:8777/console/` in a browser and sign in.

To run the backend test suite, install the development requirements first, then run `pytest`:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Platform guides: [Installation](docs/INSTALLATION.md) · [Configuration](docs/CONFIGURATION.md) · [Docker](docs/DOCKER.md) · [macOS](docs/deployment/MACOS.md) · [Windows](docs/deployment/WINDOWS.md) · [Linux](docs/deployment/LINUX.md)

## Integrating DeepSeek Harness (DSH) and other agents

### Option 1: the native DSH Desktop plugin (recommended)

1. In the console's **Agent management**, create a `DSH` agent, grant it `readwrite` on the space, and issue a `jmt_...` token. Save it to `~/.jarvismind/dsh-agent.token`. You can also run `examples/dsh-integration/python/setup_agent.py` to do this automatically.
2. In DSH Desktop, open **Plugins → Add plugin**, enter the local `plugins/dsh-jarvismind` directory and enable **JarvisMind Memory**. In the plugin settings you can switch `rememberMode` (default `agent`) and `memoryStyle` (`assistant` / `companion`).

Full steps: [plugins/dsh-jarvismind/README.md](plugins/dsh-jarvismind/README.md) and [docs/tutorials/INTEGRATE-WITH-DSH.md](docs/tutorials/INTEGRATE-WITH-DSH.md).

### Option 2: any application through the API or SDKs

1. Before replying, call `POST /api/v1/agent/context` and put the returned `<jarvismind-context>` into the system prompt as read-only reference data.
2. During the conversation, the model or a post-turn hook calls `POST /api/v1/agent/remember` to submit a memory.
3. Forgetting is a two-step flow, and the owner gives the final approval in the console.

See [docs/AGENT-INTEGRATION.md](docs/AGENT-INTEGRATION.md), the [Python SDK](sdk/python/README.md) and the [TypeScript SDK](sdk/typescript/README.md).

## Current verification status (stated plainly)

| Module / platform | Result |
| --- | --- |
| Backend test suite (including the P0 gate) | 2533 passed / 0 failed / 0 skipped; gate 67/67 |
| Web console | TypeScript 0 errors, 15 unit tests pass, production build succeeds |
| Python SDK / TypeScript SDK | 10 / 9 tests pass |
| Native DSH plugin | 79 tests pass; build clean; `SMOKE OK` against a real DSH Desktop `0.2.0-rc.2` runtime |
| DSH cross-session demo | 16/16 checks pass (Python and TypeScript, against a live server) |
| Windows (local development machine) | Backend tests and console build pass; a browser smoke passed (sign-in, dashboard, graph `ready`, light/dark theme switch with no errors). macOS and Linux have not been tested yet |
| Docker image, Linux install, independent final review | **Not done yet** (see [RELEASE-1.0-FINAL-REPORT.md](RELEASE-1.0-FINAL-REPORT.md); no `v1.0.0` tag has been created) |

> On a fresh machine with no local embedding-model cache, the first memory list can take about 40 seconds while the model download retries. Set `FASTEMBED_CACHE_PATH` or pre-cache the model to avoid this.

## Documentation index and security

* Full documentation index: [docs/README.md](docs/README.md) · [API reference](docs/API-REFERENCE.md) · [Knowledge graph](docs/KNOWLEDGE-GRAPH.md) · [Roadmap](docs/ROADMAP.md)
* Security notes and audit status: [docs/SECURITY.md](docs/SECURITY.md) · [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md)
* Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [CHANGELOG.md](CHANGELOG.md)

## License

Released under the [MIT License](LICENSE).
