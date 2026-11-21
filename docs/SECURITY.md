# Security

This document describes how JarvisMind protects memory and credentials, what you must do to keep it protected, and what
is still open. The findings of the review and their status are in [SECURITY-AUDIT.md](SECURITY-AUDIT.md). To report a
vulnerability, see [SECURITY.md](../SECURITY.md).

## Ten rules the design follows

1. **Least privilege by default.** A new agent gets three permissions (`read`, `search`, `context`). Writes, deletion,
   graph access and sensitive access must be granted explicitly.
2. **Secure defaults.** The server binds to `127.0.0.1`. Direct writes, lifecycle import and the P1 gate are off. Protected
   memories are held, not written.
3. **Sensitive data is not exposed.** Protected memories are withheld from agents without `sensitive`, redacted in
   exports, and never shown as text in receipts.
4. **Tokens are not recorded.** The owner token is never printed or logged. Agent tokens are shown once and stored only as
   HMAC-SHA256 digests keyed with a control key file.
5. **Memory text is not logged in full.** Logs and the access log hold metadata. The access log never holds bodies, query
   strings, headers, tokens or raw client addresses. Client addresses are stored only as keyed digests.
6. **Access logs are redacted.** Route templates are logged, not raw paths, so identifiers in paths do not leak.
7. **Memory permissions are enforced on the server.** Every agent route declares the permission it needs. The console and
   the SDKs display what the server allows; they enforce nothing.
8. **The front end cannot bypass governance.** The console calls the same governed endpoints as any client. There is no
   database access from the browser.
9. **Memory spaces do not leak.** Each additional space is a separate database and vector directory. Space selection
   fails closed, and an unknown space and an ungranted space produce the same answer.
10. **Agents cannot exceed their authority.** Agents cannot approve or reject, cannot change memories directly, and cannot
    request corrections. Their forget requests need the owner's decision.

## Credentials

| Credential | Created by | Stored as | Lifetime | Shown |
| --- | --- | --- | --- | --- |
| Owner | `JARVISMIND_API_TOKEN`, or generated into `data/.jarvismind_token` on first start | Environment or a file with mode 0600 | Until you change it | Never printed |
| Agent | Owner, from the console or `POST /api/v1/control/agents/{id}/tokens` | HMAC-SHA256 digest with `data/.control_key` | 1 to 3650 days, default 90 | Once, at issue or rotation |

- Rotating an agent token issues a new one with the same lifetime and revokes the old one at once.
- Revoking, expiry, disabling the agent, or deleting the agent all stop the token immediately.
- Comparison of the owner token uses a constant-time function.
- Agent tokens have the form `jmt_<public id>_<secret>`. Listings show only the public id.

## Data at rest

- The owner token file and the control key are created with mode 0600.
- `init-db` and `serve` set the process umask to 077, so the SQLite databases, their WAL files and the vector directories
  are created owner-only. Files that already exist keep their permissions. Check them after an upgrade.
- Windows does not apply POSIX modes. Restrict the data directory with NTFS permissions.

## Authorization model

- Every agent request is checked for: a valid token, an active agent, an active space, a grant on that space, and the
  permission for the route. A `write` route also needs `readwrite` on the space.
- Every owner route requires the owner credential. Agent tokens are refused on owner routes.
- Authentication runs before the route. A request that cannot select a space is refused, and nothing falls back to the
  default space.

## Rate limiting

Requests are limited per process in a 60-second sliding window: 600 per minute for the owner, 120 per minute for an agent
token and 20 failed authentications per minute per client-address digest. The counters hold at most 10 000 keys, and the
least recently used key is dropped first. Limits are per process, so a restart resets them. Limits that count client
addresses see the address of the reverse proxy when the server runs behind one. See [DEPLOYMENT notes](deployment/LINUX.md).

## Input handling

- Request bodies use strict models: unknown fields are refused on the main write models.
- Length limits apply to every free-text field. Identifiers use fixed patterns (space identifiers, backup names).
- The database layer uses the ORM and bound parameters. Raw SQL uses only fixed identifiers defined in the code, never
  request input.
- There are no file upload endpoints in 1.0 and no WebSocket endpoints.

## Browser and CORS

- The API uses bearer tokens in headers, not cookies, so cross-site request forgery does not apply to the API.
- CORS is disabled unless you list origins in `JARVISMIND_CORS_ORIGINS`. The allowed methods and headers are explicit.
- The console is served with a Content-Security-Policy that allows scripts and styles only from its own origin
  (`script-src 'self'`, `object-src 'none'`, `frame-ancestors 'none'`). Styles allow `'unsafe-inline'`, which React needs.
  The console also sends `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
- The console renders memory text as text. Recalled memory is escaped before it is placed in a context block, so a memory
  cannot close the block.
- The owner token lives in `sessionStorage` for the tab only.

## Exports and backups

- Exports redact protected content by default. Including it requires an explicit parameter and a confirmation in the
  console. Each export says what it contains.
- Backups are plain SQLite files. They are not encrypted by JarvisMind. Protect them as you would the originals.

## The eighteen review surfaces

| Surface | How it is handled | Status |
| --- | --- | --- |
| Authentication | Owner and agent tokens, constant-time owner comparison, HMAC digests, once-only display | Reviewed. |
| Authorization | Server-side permission and grant checks on every agent route; owner-only routes | Reviewed. |
| API tokens | Generation, storage, rotation, revocation, expiry, lifetime limits | Reviewed. One residual item, see the audit. |
| CORS | Disabled by default; explicit origins, methods and headers | Reviewed. |
| CSRF | Not applicable to bearer-token headers. The console keeps no cookies | Reviewed. |
| XSS | Text rendering in the console; escaping in context blocks; strict CSP | Reviewed. |
| SQL injection | ORM and bound parameters; fixed identifiers for attached backup files | Reviewed. |
| Input validation | Strict models, length limits, fixed patterns for identifiers and backup names | Reviewed. |
| Rate limiting | Sliding window, key cap, per-process scope | Reviewed. Per process only. |
| Sensitive data | Policy at write and read time, redaction in exports, no text in receipts | Reviewed. The policy is a rule set, not a classifier. |
| Audit logs | Memory audit entries; metadata-only access log with retention of 100 000 rows | Reviewed. |
| File upload | No upload endpoints | Not applicable. |
| Export | Redacted by default, row caps, explicit confirmation for sensitive text | Reviewed. |
| Import | Lifecycle import is refused unless `JARVISMIND_LIFECYCLE_IMPORT_ENABLED=1`; the import script is run by an operator | Reviewed. |
| Backup | Per-space files, validated names, verification, offline restore only; backups are not encrypted | Reviewed. Encryption is open. |
| WebSocket | No WebSocket endpoints | Not applicable. |
| Worker | Pull-based, lease-controlled, no background thread | Reviewed. |
| API exposure | `/docs` and `/openapi.json` are public and show the route list, not data | Reviewed. The public documents can be filtered at a proxy if you prefer. |

## What you must do

- Keep the owner credential secret. It can read every memory.
- Give applications agent tokens, one agent per application, with the smallest permission set.
- Keep the server on `127.0.0.1`, or put a TLS reverse proxy in front of it and restrict access there.
- Keep `data/.control_key` with `control.db`, and protect both with the same care as the memories.
- Protect backups and exports. They contain memory text.
- Rotate agent tokens regularly and revoke any token you no longer need.
- Run the full test suite after each upgrade.

## Open items

These are listed in [SECURITY-AUDIT.md](SECURITY-AUDIT.md) with their status. The most important: a sensitive value placed
under an identifier-named key in metadata is checked unless it has the exact shape of a system-generated identifier, and backups are not encrypted.

## Debug surfaces

No debug endpoint is mounted in 1.0. `backend/jarvismind/core/production_guard.py` contains guards for debugging scripts, and no module of the server imports it. The debugging probes under `backend/tests/fixtures/` are test fixtures and are not part of the running application.
