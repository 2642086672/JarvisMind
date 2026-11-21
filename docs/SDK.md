# SDK

JarvisMind ships two small client libraries for the agent API. Both wrap `/api/v1/agent/*` and nothing else. Owner and
operator routes are for the console and for scripts that hold the owner credential.

| SDK | Location | Requirements | Tests |
| --- | --- | --- | --- |
| Python | `sdk/python` (package `jarvismind_sdk`) | Python 3.9 or newer, standard library only | 10 tests (`pytest`) against a local stub server |
| TypeScript | `sdk/typescript` (package `@jarvismind/sdk`) | Node.js 18 or newer, global `fetch` | 9 tests (`npm test`, which builds first) |

Both SDKs follow the same rules:

- **No runtime dependencies.** Python uses `urllib`. TypeScript uses `fetch`.
- **The token is never printed.** `repr()` and `toString()` leave it out, and errors never include it.
- **No automatic retries.** The caller decides, using the error's `retry_after` or status.
- **Governance is not bypassed.** `remember` returns the server's receipt. `forget` only registers a request. The SDKs
  have no method that writes a memory directly.
- **Identifiers are URL-encoded** when they appear in paths.

## Python

Install from the repository:

```bash
pip install ./sdk/python
```

Use it:

```python
from jarvismind_sdk import JarvisMindClient, NotFoundError

client = JarvisMindClient("http://127.0.0.1:8777", token="jmt_...", space_id="user-42")

me = client.me()
print(me["label"], me["permissions"])

receipt = client.remember("我喜欢喝乌龙茶", conversation_id="session-123")
print(receipt["status"])            # SAVED, DUPLICATE, PROTECTED, DENIED, ...

block = client.context("我喜欢喝什么茶？", limit=6)
print(block["context"])             # wrap it as data in the system prompt

created = client.forget(receipt["memory_id"], reason="用户要求删除")
print(created["status"])            # PENDING until the owner decides

try:
    client.get_memory(receipt["memory_id"])
except NotFoundError:
    pass                            # forgotten memories are not readable by agents
```

Methods:

| Method | Endpoint | Returns |
| --- | --- | --- |
| `me()` | `GET /agent/me` | identity, permissions, spaces |
| `remember(text, conversation_id, *, message_id, turn_id, source_id)` | `POST /agent/remember` | receipt |
| `extract(conversation, conversation_id, *, context, message_id, turn_id, source_id)` | `POST /agent/extract` | extraction report |
| `forget(memory_id, *, reason)` | `POST /agent/memories/{id}/forget-requests` | the pending request |
| `update(memory_id, proposed_changes, *, reason)` | `POST /agent/memories/{id}/modification-requests` | always raises `PermissionDeniedError` (`non_human_actor_forbidden`) |
| `recall(query, *, limit=8)` | `POST /agent/recall` | ranked items |
| `context(query, *, limit=8, max_content_chars=600)` | `POST /agent/context` | context block and citations |
| `search(query, *, limit=10)` | `GET /agent/search` | envelope |
| `list_memories(*, status="ACTIVE", page=1, page_size=20)` | `GET /agent/memories` | envelope with `meta` |
| `get_memory(memory_id)` | `GET /agent/memories/{id}` | memory |
| `my_requests(*, page=1, page_size=20)` | `GET /agent/governance/requests` | envelope |
| `events(*, limit=50)` | `GET /agent/events` | envelope |
| `graph_status()` | `GET /agent/graph/status` | graph status |
| `graph_nodes(*, q=None, page=1, page_size=20)` | `GET /agent/graph/nodes` | envelope |

Errors all derive from `JarvisMindError`, which has `status`, `code`, `details` and `retry_after`:

| Class | HTTP |
| --- | --- |
| `NetworkError` | the server could not be reached |
| `AuthenticationError` | 401 |
| `PermissionDeniedError` | 403 |
| `NotFoundError` | 404 |
| `ConflictError` | 409 |
| `ValidationFailedError` | 422 |
| `RateLimitedError` | 429, `retry_after` in seconds |
| `ServerError` | 5xx |

## TypeScript

Build the package from the repository:

```bash
cd sdk/typescript
npm install
npm run build
```

Use it (ES modules, Node 18 or newer, or a browser with `fetch`):

```ts
import { JarvisMindClient, JarvisMindError } from "@jarvismind/sdk";

const client = new JarvisMindClient({ baseUrl: "http://127.0.0.1:8777", token, spaceId: "user-42" });

const receipt = await client.remember({ text: "我喜欢喝乌龙茶", conversationId: "session-123" });
const block = await client.context("我喜欢喝什么茶？", { limit: 6 });

try {
  await client.update(receipt.memory_id ?? "", { content: "x" }, "更正");
} catch (error) {
  if (error instanceof JarvisMindError && error.status === 403) {
    // agents cannot request corrections; the owner makes them in the console
  }
}
```

Methods mirror the Python names in camelCase: `me`, `remember({ text, conversationId, ... })`,
`extract({ conversation, conversationId, ... })`, `forget(memoryId, reason?)`, `update(memoryId, proposedChanges, reason?)`,
`recall(query, { limit })`, `context(query, { limit, maxContentChars })`, `search(query, { limit })`,
`listMemories({ status, page, pageSize })`, `getMemory(id)`, `myRequests({ page, pageSize })`, `events({ limit })`,
`graphStatus()`, `graphNodes({ q, page, pageSize })`.

Errors are `JarvisMindError` with `status`, `code`, `details` and `retryAfterSeconds`. A network failure has the code
`network_error` and status `0`.

## Choosing a space

Both clients accept an optional space identifier and send it as `X-JarvisMind-Space`. Without it, the server uses the
agent's default space. A space the agent has no grant for returns `403 space_access_denied`.

## Adapters

An adapter is a small module in the application that calls `context` before a reply and `remember` after it. The DSH
example is the reference: `examples/dsh-integration/python/dsh_hooks.py` and `examples/dsh-integration/typescript/demo.mjs`.
See [AGENT-INTEGRATION.md](AGENT-INTEGRATION.md#adapters) for the list of patterns and what each one covers.

## Versioning

The SDK versions follow SemVer. The 1.0.0 SDKs target API version 1.0.0. A change that removes a method or changes a
returned field's meaning would be a major version.
