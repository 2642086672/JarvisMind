# API v1

JarvisMind exposes one HTTP API under `/api/v1`. The complete route list, generated from the running server, is in
[API-REFERENCE.md](API-REFERENCE.md). The OpenAPI document is at `/openapi.json`, and the interactive page is at `/docs`.

## Base URL and versions

- Base URL: the address of your server, for example `http://127.0.0.1:8777`.
- Version: the path prefix `/api/v1`. The server reports `1.0.0` from `/health` and `/api/v1/system/info`.
- Compatibility: within `v1`, new fields and new endpoints may be added. Existing fields, error codes and their meanings
  are not changed. A breaking change would start a `v2` prefix.

## Two surfaces

| Surface | Who calls it | Credential | Routes |
| --- | --- | --- | --- |
| Agent | Applications and agents | Agent token (`jmt_...`) | `/api/v1/agent/*` |
| Owner | The console and operators | Owner credential | Every other route under `/api/v1`: memories, candidates, forget and modification requests, lifecycle, graph, governance, control plane, console, operations, exports and backups |

- An agent token is refused on every owner route with `owner_only`.
- The owner credential is accepted on every owner route. On the agent routes it is refused with
  `permission_denied` (`details.reason` = `agent_credential_required`), except `GET /api/v1/agent/me`, which describes
  whichever credential was sent.
- The owner credential can read and change every memory in every space. Keep it secret, and give applications agent
  tokens only.

## Authentication

Send one credential on every request:

```http
Authorization: Bearer jmt_<public-id>_<secret>
```

The owner credential may also be sent as `X-API-Token: <token>`. A missing or wrong credential returns `401`. Failed
attempts are limited per client address (see Rate limits).

## Memory spaces

Every request runs in one memory space. Choose it with the header:

```http
X-JarvisMind-Space: user-42
```

Without the header, the space is the agent's default space (for the owner, `default`). An agent can use only the spaces
it holds a grant for. An unknown space and an ungranted space give the same answer, so space names cannot be guessed.

Space identifiers are 2 to 63 characters: lowercase letters, digits, `_` and `-`, starting with a letter or a digit.

## Envelopes

Single results:

```json
{ "data": { "id": "3f1c...", "status": "ACTIVE" } }
```

Agent lists:

```json
{ "data": [ { "id": "..." } ], "meta": { "page": 1, "page_size": 20, "returned": 1, "has_more": false } }
```

Owner paged lists return `meta.page`, `meta.page_size`, `meta.total` and `meta.total_pages` instead.

## Errors

Every error has the same shape:

```json
{
  "error": {
    "code": "permission_denied",
    "message": "This memory is sensitive and the credential lacks sensitive access",
    "details": { "code": "sensitive_access_denied", "permission": "sensitive" }
  }
}
```

`error.code` is the general class of the error. `details.code`, when present, names the specific reason (for the agent-credential refusal, the reason is in `details.reason`). Branch on
`details.code` when the general code is not enough.

| HTTP | `error.code` | Typical `details.code` | Meaning |
| --- | --- | --- | --- |
| 401 | `unauthorized` | none | No valid credential. |
| 403 | `permission_denied` | `sensitive_access_denied` (in `details.code`), `agent_credential_required` (in `details.reason`) | The credential lacks the permission, or the memory is protected and the agent lacks `sensitive`. |
| 403 | `owner_only` | none | An agent token was used on an owner route. |
| 403 | `space_access_denied` | none | No grant for the requested space. |
| 403 | `forbidden` | `space_not_selected` | The request selected no memory space. Clients never see this in normal use. |
| 403 | `non_human_actor_forbidden` | none | An agent asked for a human-only action, such as a correction or an approval. |
| 403 | `direct_write_disabled` | none | Direct writes are off (`JARVISMIND_DIRECT_WRITE_ENABLED`). |
| 404 | `not_found` | `memory_not_found` | The memory does not exist, was forgotten, or is not readable by this credential. |
| 404 | `space_not_found`, `agent_not_found`, `token_not_found` | none | A control-plane object does not exist or is not visible. |
| 409 | `conflict` | `forget_already_pending`, `agent_request_pending`, `space_in_use`, `forget_request_race` | The request conflicts with the current state. |
| 422 | `validation_error` | none | The body or a parameter is invalid. `details` lists the fields. |
| 429 | `rate_limited` | none | Too many requests. `Retry-After` gives the wait in seconds. |
| 5xx | `internal_error`, `database_error`, `search_unavailable`, `embedding_unavailable`, `vector_index_error` | none | A server-side failure. The server logs the cause without secrets. |

## Rate limits

Limits apply per process, in a sliding 60-second window:

- owner credential: 600 requests per minute by default;
- each agent token: 120 requests per minute by default;
- failed authentication attempts per client address: 20 per minute by default.

A refused request returns `429` with `Retry-After`. The values are set by `JARVISMIND_RATE_LIMIT_OWNER_PER_MINUTE` and `JARVISMIND_RATE_LIMIT_AGENT_PER_MINUTE` and
`JARVISMIND_AUTH_FAILURE_LIMIT_PER_MINUTE` in [CONFIGURATION.md](CONFIGURATION.md#rate-limits).

## Pagination and time

- `page` starts at 1. `page_size` is 1 to 100 unless a route states a smaller maximum.
- Times are ISO 8601 in UTC, for example `2026-11-21T18:29:10.037673+00:00`.
- Identifiers are opaque strings. Do not parse them.

## The agent surface

All agent routes need an agent token. The permission in the last column is required, and it is checked on the server
(see [AGENT-INTEGRATION.md](AGENT-INTEGRATION.md#permissions)).

| Method and path | Purpose | Permission |
| --- | --- | --- |
| `GET /api/v1/agent/me` | Identity, permissions, usable spaces and the active space. | none |
| `POST /api/v1/agent/remember` | Submit a statement through governance. Returns a receipt. | `write` |
| `POST /api/v1/agent/extract` | Extract candidate memories from a transcript. Candidates still pass governance. | `write` |
| `POST /api/v1/agent/recall` | Ranked memories for a query. Body: `query`, `limit` (1 to 20). | `search` |
| `GET /api/v1/agent/search?q=...&limit=` | Keyword search over readable memories. `limit` is 1 to 50. | `search` |
| `POST /api/v1/agent/context` | A context block wrapped as untrusted data, plus citations. Body: `query`, `limit` (1 to 20), `max_content_chars` (80 to 2000). | `context` |
| `GET /api/v1/agent/memories?status=&page=&page_size=` | Readable memories, newest first. `status` is `ACTIVE` or `ARCHIVED`. | `read` |
| `GET /api/v1/agent/memories/{id}` | One readable memory. | `read` |
| `POST /api/v1/agent/memories/{id}/forget-requests` | Ask the owner to forget a memory. Never returns a confirmation token. | `delete` and `write` on the space |
| `POST /api/v1/agent/memories/{id}/modification-requests` | Always refused with `non_human_actor_forbidden`. | `update` |
| `GET /api/v1/agent/governance/requests` | This agent's forget and modification requests, with their statuses. | `read` |
| `GET /api/v1/agent/graph/status` | Whether the graph is usable in this space. | `graph` |
| `GET /api/v1/agent/graph/nodes?q=&page=&page_size=` | Graph entities whose source memory is still valid. | `graph` |
| `GET /api/v1/agent/events?limit=` | This agent's own API access records (metadata only). | none |

### Examples

Recall for a query:

```bash
curl -s -X POST http://127.0.0.1:8777/api/v1/agent/recall \
  -H "Authorization: Bearer $AGENT_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "我喜欢喝什么茶", "limit": 5}'
```

Submit a statement:

```bash
curl -s -X POST http://127.0.0.1:8777/api/v1/agent/remember \
  -H "Authorization: Bearer $AGENT_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "我喜欢喝乌龙茶", "conversation_id": "session-123"}'
```

Request a forget. The owner decides:

```bash
curl -s -X POST http://127.0.0.1:8777/api/v1/agent/memories/$MEMORY_ID/forget-requests \
  -H "Authorization: Bearer $AGENT_TOKEN" -H "Content-Type: application/json" \
  -d '{"reason": "用户要求删除"}'
```

Only the owner can approve it, with `POST /api/v1/forget-requests/{id}/approve` and the owner credential. The console
does this from the approval center.

## The owner surface

The owner surface is what the console uses. Its routes cover memories, candidates, modification and forget requests,
lifecycle actions, the graph, governance status, control-plane management, operations, exports and backups. Every
route is listed in [API-REFERENCE.md](API-REFERENCE.md). Three routes are worth knowing:

- `POST /api/v1/control/agents`: create an agent.
- `POST /api/v1/control/agents/{id}/tokens`: issue its token. The response contains the token once. Only the HMAC
  digest is kept.
- `POST /api/v1/console/backups`: create a backup of the selected space. See [BACKUP.md](BACKUP.md).

## Versioning of this document

This document describes version 1.0.0 of the API. The generated reference lists every route of the running server.
If the two disagree, the running server is correct and this document needs an update.
