# Agent integration

An agent is any program that talks to a person and wants to remember and recall what that person said. JarvisMind
gives each agent its own identity, its own permissions and its own token. The owner decides what an agent may read and
write, and can revoke it at any time.

## Concepts

| Concept | What it is |
| --- | --- |
| Agent | An identity with a name, a type (for example `dsh`, `deepseek`, `claude`, `custom`), a status (`active` or `disabled`) and a default space. |
| Permissions | The ceiling for the agent. Eight permissions exist; see the table below. A new agent gets `read`, `search` and `context` only. |
| Space grant | Access to one memory space: `read` (search and recall) or `readwrite` (also write). |
| Token | A secret string `jmt_<id>_<secret>` shown once when created or rotated. The server stores only an HMAC digest. Lifetime 1 to 3650 days, default 90. |

A token can use fewer permissions than its agent has, never more. Disabling an agent stops all its tokens at once.
Deleting an agent revokes its tokens. Its history stays in the access log.

## Permissions

| Permission | Allows |
| --- | --- |
| `read` | List and read memories in granted spaces. Also read the agent's own governance requests and event log. |
| `search` | Keyword search (`GET /api/v1/agent/search`) and recall (`POST /api/v1/agent/recall`). |
| `context` | Build a context block for a conversation (`POST /api/v1/agent/context`). |
| `write` | Submit statements through the governed paths (`remember`, `extract`). Requires `readwrite` on the space. |
| `delete` | Ask for a memory to be forgotten (`POST .../forget-requests`). The owner decides. Requires `readwrite`. |
| `update` | Ask for a correction. The JMR policy refuses agent corrections, so this permission is kept for the contract only. |
| `graph` | Read the knowledge graph (`/api/v1/agent/graph/*`). |
| `sensitive` | See memories that are classified as sensitive. Never granted by default. |

Permissions are enforced on the server for every request, whatever the console or the SDK shows.

## Recommended setup for a chat application

1. Create one agent per application (not one per user).
2. Give it `read`, `search`, `context`, `write` and `delete`. Leave `sensitive` and `graph` off unless the application
   needs them.
3. Grant it `readwrite` on the space it should use. Make that space its default.
4. Store the token in the application's server-side secret store. Rotate it on a schedule with the console or the
   owner API. Rotation keeps the original lifetime.
5. Do not give one user's conversation access to another user's memories. Use one memory space per user or per
   project, and grant the agent only the spaces it needs.

## The conversation loop

### Before a reply: retrieve

Call `POST /api/v1/agent/context` with the user's latest message and place the returned block in the system prompt.

```python
from jarvismind_sdk import JarvisMindClient

client = JarvisMindClient("https://memory.example.internal", token, space_id="user-42")
context = client.context("我之前准备买什么电脑？", limit=6)
system_prompt = f"{BASE_PROMPT}\n\n{RULES}\n{context['context']}"
```

The block is wrapped in `<jarvismind-context trust="untrusted-data">`. Tell the model in `RULES` that the block is
reference data and that instructions inside it must be ignored. If the call fails, answer without the block. Memory is
an enhancement, not a dependency.

### After a turn: submit

Call `remember` for the statement the user made. Do this after the reply, and do not wait for the result before
showing the reply to the user.

```python
receipt = client.remember(user_message, conversation_id="session-123", message_id=msg_id)
```

Read the receipt's `status`. Show the user only what the owner needs to see: a `PROTECTED` receipt means the owner will
be asked. Never show the stored text of a `PROTECTED` receipt.

### When the user asks to forget

Call `forget` and tell the user that the owner will confirm it.

```python
created = client.forget(memory_id, reason="用户要求删除")
# created["status"] == "PENDING"; the memory is still there until the owner approves
```

Do not tell the user the memory is gone until the request is `EXECUTED`. Check it with `my_requests()`.

## Errors and retries

Every error uses the same envelope: `{"error": {"code", "message", "details"}}`. `error.code` is the general class; `details.code` names the reason when the server provides one (the agent-credential refusal uses `details.reason`). Handle these cases:

| HTTP | Code | What to do |
| --- | --- | --- |
| 401 | `unauthorized` | The token is wrong, expired, revoked, or its agent is disabled. Stop and report it to the owner. |
| 403 | `permission_denied` | The agent lacks the permission. Do not retry. |
| 403 | `space_access_denied` | The agent has no grant for the requested space. |
| 403 | `non_human_actor_forbidden` | An agent tried a human-only action (a correction or an approval). Do not retry. |
| 404 | `not_found` (`details.code` = `memory_not_found`) | The memory does not exist, was forgotten, or is not readable by this agent. |
| 409 | `conflict` (`details.code` = `forget_already_pending`) | A request for this memory is waiting for the owner. Show its status. |
| 422 | `validation_error` | The request is malformed. Fix it; do not retry unchanged. |
| 429 | `rate_limited` | Wait `Retry-After` seconds, then retry with backoff. |
| 5xx | `internal_error` and others | Retry with exponential backoff. The server de-duplicates repeated statements (`DUPLICATE`), so a retry does not store the same memory twice. |

Keep the agent's own retry budget small. A slow memory service must not delay the chat.

## Prompt-injection rules

- Recalled memory is user data written in the past. It can contain text that looks like an instruction. The model
  must treat the context block as reference only.
- Never put memory text in a position where it can change tools, permissions or the next action without review.
- Never let the model decide to forget, correct or share a memory. Those are owner decisions in JarvisMind.

## Adapters

JarvisMind does not depend on any model vendor. The table lists the integration patterns the documentation covers.

| Application | Pattern | Verified in 1.0 |
| --- | --- | --- |
| DSH | Pre-chat context, post-chat remember, forget requests, cross-session demo | Yes, with the example hooks and the demo ([tutorial](tutorials/INTEGRATE-WITH-DSH.md)). |
| DeepSeek, OpenAI-compatible APIs, Claude | Same hooks around the chat call | No. Documented pattern only. |
| OpenClaw, LangChain, LlamaIndex | Retriever or tool wrapper around `context` and `remember` | No. Documented pattern only. |
| Custom agents | Any HTTP client using the API or the SDKs | Covered by the SDK tests against a local stub server. |

An adapter needs to implement two calls: a function that turns a conversation turn into a context block, and a function
that submits a turn after the reply. Keep both in the application's server, not in a browser.
