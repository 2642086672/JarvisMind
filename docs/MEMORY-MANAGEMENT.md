# Memory management

A memory is a short statement that JarvisMind keeps about a user, a project or the world, with the metadata needed to
recall, govern and forget it. JME is the only store of memory text.

## Fields

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | Assigned by the server. |
| `memory_type` | enum | `PROFILE`, `SEMANTIC`, `EPISODIC`, `PROJECT`, `WORKING`, `REFLECTION`. |
| `content` | string | The memory text, up to 100 000 characters. |
| `summary` | string or null | Optional short form, up to 5 000 characters. |
| `status` | enum | `ACTIVE`, `ARCHIVED`, `DELETED`, `MERGED`. See below. |
| `importance` | number | 0 to 10. Default 5 for direct creation. |
| `confidence` | number | 0 to 1. |
| `source_id` | string or null | Caller-defined source reference, up to 128 characters. |
| `created_at`, `updated_at`, `accessed_at` | ISO 8601 UTC | Set by the server. |
| `expires_at` | ISO 8601 or null | When set, the memory expires. |
| `metadata` | object or null | Free structured data. Checked by the credential and sensitivity rules. |
| `forgotten_at`, `forget_reason` | set when forgotten | Recorded by the forget execution. |
| `merged_into` | string or null | Set on a memory that was merged into another. |

Sensitivity is not a stored field. It is decided from the content, the summary and the metadata when a memory is
written and each time it is read. Candidates store the categories that matched.

## Statuses

| Status | Meaning | Visible to agents |
| --- | --- | --- |
| `ACTIVE` | Current and usable. | Yes, subject to permissions and sensitivity. |
| `ARCHIVED` | Kept, not used for recall. Can be restored. | Yes, through the archived filter. |
| `DELETED` | Forgotten through the two-step flow. Text is retained only as a record of the decision. | Never. |
| `MERGED` | Kept as provenance after a merge. | Never. |

## Creating memories

- **Through an agent or a statement**: `POST /api/v1/agent/remember`. The text goes through the governance rules in
  [GOVERNANCE.md](GOVERNANCE.md). Most statements are stored; sensitive ones wait for the owner.
- **Through the console**: the "新建记忆" button calls `POST /api/v1/memories/auto-remember`. It uses the same rules.
- **Directly**: `POST /api/v1/memories` writes a row with no governance step. It is refused unless
  `JARVISMIND_DIRECT_WRITE_ENABLED=1`. Keep it off for normal use.

## Reading and searching

The console Memory Explorer lists memories by status (ACTIVE, ARCHIVED, DELETED, MERGED), by type, and sorted by
creation time, update time or importance. Keyword search runs on the server. When vectors are enabled, hybrid search
(`GET /api/v1/memories/hybrid-search`) combines the keyword and vector rankings.

The owner's list endpoint is `GET /api/v1/memories` with `status`, `memory_type`, `sort_by`, `sort_order`, `page` and
`page_size` (up to 100). Keyword search is `GET /api/v1/memories/search?q=...`.

## Editing

- **Unprotected memories** can be edited directly (`PATCH /api/v1/memories/{id}`) when direct writes are enabled. The
  editable fields are `content`, `summary`, `importance`, `confidence`, `status`, `source_id`, `expires_at` and
  `metadata`.
- **Protected memories** are corrected through a modification request. The owner approves the frozen values. See
  [GOVERNANCE.md](GOVERNANCE.md#6-corrections-use-modification-requests).
- **Classification (memory type)** cannot be changed in 1.0. The update endpoint does not accept it.

## Archive and restore

Archive and restore are lifecycle actions on a single memory. Their exact paths are listed under the
`memory-lifecycle` group of [API-REFERENCE.md](API-REFERENCE.md). An archived memory is kept and can be restored.

## Forgetting

Forgetting is always a two-step decision by the owner. The memory is not changed when a request is created. The owner
approves it, and only then the memory becomes `DELETED`, its vector entry is removed, and the knowledge graph stops
using it. Details and the statuses of a forget request are in [GOVERNANCE.md](GOVERNANCE.md#5-forgetting-is-two-step).

After a memory is forgotten:

- `GET /api/v1/agent/memories/{id}` returns `404`.
- Recall, context, search and listing for agents never return it.
- The owner can still see that it was forgotten, with the decision in the audit log.

## Merging

Merging is a lifecycle action. A preview shows what would change. Confirming it creates one memory and marks the
originals `MERGED`. The originals are kept as provenance and are never returned to agents. Merge routes are listed
under `memory-lifecycle` in the API reference.

## History

`GET /api/v1/memories/{id}/audit-logs` returns the audit entries of one memory: creation, updates, receipts, forget
requests and their decisions, with the actor label and the time.

## Export

The console exports three files:

- Memories, as JSON or CSV, with `status` (ACTIVE, ARCHIVED or DELETED). At most 5 000 rows per export. Sensitive
  content is redacted unless the owner asks for it.
- The knowledge graph, as JSON. At most 1 000 nodes and 1 000 edges. Protected content is omitted unless requested.
- The memory audit log, as JSON, with the API access log summary. Details are redacted unless requested.

Each export says whether it contains sensitive text and whether it was truncated. See [BACKUP.md](BACKUP.md).

## Not available in the 1.0 console

These items appear on the pages as **Not Available**, with the reason. Nothing is estimated.

- Tags, source filters, and time-range filters beyond creation time.
- Sensitivity filters in the list. Sensitivity is applied to what each reader may see.
- Changing the memory classification.
- Bulk delete and bulk edit.
- Related memories, and a per-memory access history. The access log records routes, not memory identifiers.
- A per-memory governance record across all decisions. Use the governance center for the open items.
