# Governance (JMR)

JMR decides what may become a memory, who may change one, and who may forget one. Every rule below is enforced by
the server. The console and the SDKs display decisions; they do not make them.

## Receipts: what happened to a statement

`POST /api/v1/agent/remember` returns a receipt with a `status`. The table lists every status the 1.0 server returns.

| Status | Stored? | Meaning |
| --- | --- | --- |
| `SAVED` | yes | A new memory was written, with its audit entry. |
| `UPDATED` | yes | An existing memory was changed by the statement. |
| `DUPLICATE` | no | The statement is at least 0.82 similar to an existing memory. |
| `CONFLICT` | no (candidate) | The statement contradicts an existing memory. A candidate waits for the owner. |
| `PROTECTED` | no (candidate) | Personal sensitive content. Held for the owner's authorization. The receipt shows a redacted text. |
| `DENIED` | no | The text contains a credential. Refused before anything else happens. |
| `NO_INTENT` | no | No explicit memory request was found in the text. |
| `NO_MEMORY` | no | The request was a question, a hypothetical, quoted speech, or not a fact. |
| `FAILED` | no | The write could not be completed. Nothing partial is kept. |

The receipt contains a message in plain language. It never contains a credential.

## Rules

### 1. Credentials are refused before any write

Keys, tokens and passwords are detected in text and in structured metadata (the metadata, source and expiry fields of
write requests are checked recursively). Such a write is refused, the value is never echoed back, and the attempt is
recorded without the secret.

### 2. Personal sensitivity is held for the owner

Two detectors run on every explicit statement and on every stored memory at read time:

- Personal categories: health, home address, finances, sexual orientation, religion, politics. These are
  keyword and pattern rules, so a memory is flagged only when a rule fires. A rule that does not fire is not a proof
  that the content is safe.
- Personal identifiers such as phone numbers, detected by the credential and identifier detector.

Protected content is not written as a memory. It becomes a candidate with `PENDING_AUTHORIZATION`, and the owner
decides in the approval center. The candidate stores the categories that matched, not the matched values.

Visibility for agents:

- An agent without the `sensitive` permission never gets a protected memory from list, get, search, recall or
  context calls.
- Export redacts protected content unless the owner explicitly asks for it (`include_sensitive=true`).

### 3. Duplicates and conflicts

A new statement at least 0.82 similar to an existing memory returns `DUPLICATE` and is not stored again. The
threshold is fixed at 0.82 in `backend/jarvismind/core/dedup.py` and is not configurable. The JMR reflection module has its own threshold, `JARVISMIND_JMR_DUPLICATE_THRESHOLD`. A statement that conflicts with an existing memory becomes a
`CONFLICT` candidate.

### 4. Only humans decide

Agent identities are non-human actors under JMR.

- An agent can ask for a memory to be forgotten. The owner decides.
- An agent cannot approve or reject any request, including its own.
- An agent cannot request a correction. `POST /api/v1/agent/memories/{id}/modification-requests` is refused with
  `non_human_actor_forbidden`. Corrections are made by the owner.
- An agent cannot choose the origin label of a request. The label `agent:` is reserved for requests the agent
  facade creates on the server side. Public routes refuse it.

### 5. Forgetting is two-step

The lifecycle of a forget request is:

| Status | Meaning | Memory |
| --- | --- | --- |
| `PENDING` | Waiting for the owner. | Unchanged. |
| `APPROVED` | The owner approved, and execution has started but is not confirmed. | Not yet deleted. A request stuck here must not be approved again; an operator reconciles it. |
| `EXECUTED` | Terminal. The memory was marked `DELETED` and the execution is recorded. | `DELETED`. |
| `REJECTED` | Terminal. The owner declined. | Unchanged. |
| `FAILED` | Terminal. Execution rolled back. A new request is needed to try again. | Unchanged. |

An owner request returns its one-time confirmation token once, at creation. The owner approves with that token. An
agent request never returns a token, so the server decides approval of agent requests from the origin it recorded
when the request was created.

Only the owner can approve or reject. There is one pending forget request per memory. A second request from the same
agent returns the existing one, and a request from a different party returns a conflict (`forget_already_pending`).

### 6. Corrections use modification requests

Direct edits (`PATCH /api/v1/memories/{id}`) are refused unless `JARVISMIND_DIRECT_WRITE_ENABLED=1`. A memory that came
through the approval flow is changed by a modification request: the owner's approval applies exactly the values frozen
in the request, not values supplied with the approval.

### 7. Lifecycle actions have their own switches

| Action | Default | Why |
| --- | --- | --- |
| Administrative import | off | It can create memories with chosen IDs and statuses. |
| Merge | on | Requires two existing memories and produces one product. The originals are kept as `MERGED`. |
| Summary review | on | The owner confirms summaries the system produced. |
| Correction resolve | on | The owner confirms corrections the system produced. |

## Approval center (console)

The approval center lists the work that waits for the owner.

- **Candidates**: `PENDING`, `PENDING_AUTHORIZATION`, `APPROVED`, `REJECTED`.
- **Forget requests**: the statuses above.
- **Modification requests**: `PENDING`, `APPROVED`, `REJECTED`, `EXECUTED`, with the frozen values shown.

The console shows a forget request for approval only when the server recorded an agent origin. A request from another
owner session can be decided only in the session that created it, because its one-time token was shown only there.

## Audit

Each decision writes an entry to `memory_audit_logs` with the action, the actor label, the memory and the detail.
`GET /api/v1/memories/{id}/audit-logs` returns a memory's history. The console exports the audit log, with details
redacted unless the owner asks for sensitive text.

The access log is a separate table. It records metadata only: method, route template, status, latency, principal kind,
agent id, token id, space, error code and a keyed digest of the client address. It never records request bodies,
query strings, headers, tokens or raw addresses.

## Policies and their configuration

Policies are set in configuration, not in a rules table. See [CONFIGURATION.md](CONFIGURATION.md#governance). The
governance overview in the console shows the active values read-only.

## Not in 1.0

- Creating, editing, versioning, enabling or disabling governance rules, and running rule tests. Policies are
  configuration values in 1.0.
- Editing the memory classification. The categories are fixed. Memory types are the six JME types.
- An approval expiry and a separate authorization history. The audit log is the history.
