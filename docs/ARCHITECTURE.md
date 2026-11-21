# Architecture

## Principles

1. **One authority.** JME owns every memory row. Other components read it, or propose changes through
   governed paths. Nothing keeps an independent authoritative copy.
2. **Governance before writes.** Every write path passes the credential gate, the sensitivity
   classification, and the duplicate and conflict checks before a memory row exists.
3. **Derived views.** The knowledge graph and the vector index are derived. Both can be rebuilt from
   memories and the memory audit log.
4. **Physical isolation per memory space.** Each additional memory space is a separate SQLite database
   and a separate vector directory. One query cannot reach another space's rows.
5. **Least privilege for agents.** An agent credential carries explicit permissions and per-space grants.
   The server checks both on every request.
6. **Data, not instructions.** Recalled memory is returned inside a context block marked
   `trust="untrusted-data"`, with markup escaped, so it cannot close the block or inject markup.

## Components

| Component | Responsibility | Main code |
| --- | --- | --- |
| JME, memory engine | Memory create/read/update, keyword and hybrid search, lifecycle (archive, restore, expiry), audit log, vector index | `backend/jarvismind/services/memory_service.py`, `backend/jarvismind/database/`, `backend/jarvismind/repositories/` |
| JMR, governance | Credential gate, personal-sensitivity policy, duplicate and conflict checks, approval center (candidates, forget requests, modification requests), audit | `backend/jarvismind/services/auto_memory.py`, `services/approval_service.py`, `services/forget_service.py`, `core/`, `jmr/` |
| JKG, knowledge graph | Entities and relations derived from memories, query API, lifecycle sync from the audit log | `backend/jarvismind/jkg/`, `backend/jarvismind/api/routes/jkg.py` |
| MCE, context engine | Retrieval, relevance floor, context block, exclusion counts, citations | `backend/jarvismind/services/memory_context_service.py`, `backend/jarvismind/mce/`, `backend/jarvismind/api/v1/agent.py` |
| Control plane | Memory spaces, agents, grants, API tokens (HMAC digests), access log (metadata only) | `backend/jarvismind/control/`, `backend/jarvismind/spaces.py` |
| API v1 | Owner surface (console) and agent surface, envelopes, errors, rate limits | `backend/jarvismind/api/` |
| Console | Owner UI. Every page calls the API. No database access, no memory copy | `console/src/` |
| SDKs and examples | Python and TypeScript agent clients, DSH hooks and demos | `sdk/`, `examples/dsh-integration/` |

JME, JMR, JKG and MCE run in one process. They are separate modules with one-way dependencies:
JME never imports JKG, and the console never imports any of them.

## Data layout

The data directory is `JARVISMIND_DATA_DIR` (default `data`, relative to the directory you start the
server from).

```text
data/
  .jarvismind_token        owner token file (mode 0600), used when JARVISMIND_API_TOKEN is not set
  .control_key             32-byte key for HMAC digests of agent tokens and client addresses (mode 0600)
  control.db               control plane: spaces, agents, grants, token digests, access log (metadata only)
  jarvismind.db            default memory space: JME, JMR and JKG tables
  qdrant/                  default space vector index (local, embedded)
  models/                  embedding model cache
  backups/                 default space backups (created from the console or the backup script)
  spaces/<space-id>/       one directory per additional memory space
    jarvismind.db
    qdrant/
    backups/
```

Deleting `.control_key` invalidates every agent token, because their digests can no longer be checked.
Keep it with the control database and back both up together.

## Request path

```text
client
  -> access log (metadata only; written after the response)
  -> authentication: owner credential, or agent token (jmt_...)
  -> memory space selection: X-JarvisMind-Space header, or the agent's default space
  -> permission and grant checks (agents), owner-only checks (console and operator routes)
  -> route handler -> service -> repository -> SQLite (and the vector index)
```

Authentication happens before the route runs. A request that cannot select a memory space is refused
with `space_not_selected`. It is never served from the default space by accident.

## Write path: an explicit "remember" request

`POST /api/v1/agent/remember` (and the console's create action) runs these steps in order:

1. **Intent.** Only explicit statements are considered. Questions, hypotheticals and quoted speech produce
   `NO_INTENT` or `NO_MEMORY`.
2. **Credential gate.** Keys, tokens and passwords are refused before anything is stored. The receipt is
   `DENIED` and the secret is never echoed back.
3. **Sensitivity.** Personal categories (health, home address, finances, sexual orientation, religion,
   politics) and personal identifiers (for example phone numbers) are not written. They are held as a
   candidate, and the receipt is `PROTECTED`. The owner decides whether they become memories.
4. **Duplicate and conflict.** A near-identical memory (similarity of at least 0.82 by default) gives
   `DUPLICATE`. A conflicting statement gives `CONFLICT` and a candidate for review.
5. **Write.** Otherwise the memory row is written together with its audit entry. The receipt is `SAVED`,
   or `UPDATED` when an existing memory was changed.

The receipt always states the status and a short message. The stored text is shown only when it was
actually stored or held.

## Forget path: two steps, decided by the owner

1. **Request.** The owner or an agent asks to forget a memory. This creates a `PENDING` forget request.
   The memory is not changed. An owner request returns a one-time confirmation token once. An agent
   request never returns a token.
2. **Decision.** The owner approves or rejects. Approval of an agent request needs no token, because the
   server created that request on the agent path and recorded its origin. Any other request must present its
   token.
3. **Execution.** The memory becomes `DELETED` (a soft delete), its vector entry is removed, an audit entry
   is written, and the graph is synchronized from the audit feed. Agents never receive a `DELETED` memory
   from any read or recall endpoint.

## Context path: recall for a model

`POST /api/v1/agent/context` does this:

1. Retrieves candidates with keyword search and, when an embedding provider is configured, vector search.
   The two rankings are fused with reciprocal rank fusion. If vectors are unavailable the result is marked
   `degraded_to_fts`.
2. Applies a relevance floor, excludes expired, deleted and merged memories, and withholds sensitive memories
   from agents without the `sensitive` permission.
3. Returns a context block, the citations for the memories included, and counts of what was excluded.

## Knowledge graph

The graph is built from memories and kept in step through the memory audit log. It has its own tables
(`jkg_*`), its own switch (`system_settings['jkg.enabled']`), and its own API. It is never an authority:
if a source memory is forgotten, the edges that depend on it stop being valid. See
[KNOWLEDGE-GRAPH.md](KNOWLEDGE-GRAPH.md).

## Known architecture limits

- One process. Rate-limit counters live in memory and reset on restart.
- One SQLite database per memory space. SQLite serializes writers, so this suits a single-node installation.
  The control database and every space database use WAL mode, foreign-key enforcement and a 5-second busy timeout.
- The vector index is embedded Qdrant, one per space. There is no shared index service.
- There are no user accounts. One owner credential administers the installation. Agents are the only
  multi-tenant identities.
- The console keeps the owner token in `sessionStorage` only. It stores no memory copy.
- Docker, Windows and Linux deployments are documented but were not run in the release environment.
  See the release report.
