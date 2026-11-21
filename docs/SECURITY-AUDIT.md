# Security audit

This document records the security review of JarvisMind 1.0.0: what was reviewed, what was found, what was fixed, and what
remains open. It is a statement of evidence, not a certification.

## Method

1. **Self-review** during the build. Each control in [SECURITY.md](SECURITY.md) was checked against the code, and the
   tests that cover it were identified.
2. **Two read-only reviews** by a separate reviewer (a model-based reviewer, authorized for this purpose). The reviewer
   read the source, ran probes against the application in a scratch directory outside the repository, and reported
   findings with evidence. The reviewer did not change files.
3. **Fixes** were made by the author. Each fix has a test or a probe. The full test suite was run after the fixes.

The reviews did not include a penetration test, a fuzzing campaign, a dependency vulnerability scan or a review of the
deployment environment. Those are open items.

Severity:

| Level | Meaning | Release rule |
| --- | --- | --- |
| P0 | An agent or unauthenticated party can read, change or forget memory it should not. | Must be fixed before release. |
| P1 | A control does not work as designed, or a sensitive path is open. | Must be fixed before release. |
| P2 | A weakness with limited impact, or a defence-in-depth gap. | Fix, or document a mitigation. |
| P3 | Hardening and consistency. | Fix when convenient. |

## Round 1: control plane, spaces, tokens and access

| ID | Severity | Finding | Status | Evidence |
| --- | --- | --- | --- | --- |
| R1-01 | P0 | The owner-authority branch of forget approval was derived from the requester's own label. A request that claimed `agent:` could be approved by an owner route without a token. | Fixed | The label `agent:` is reserved and set by the server. Public routes refuse it. Owner approval without a token needs a server-recorded agent origin. `tests/test_agent_facade.py`, `tests/test_security_followups.py`. |
| R1-02 | P1 | The control plane was not wired into the application. Owner tokens were used for data routes, and agent routes did not exist. | Fixed | `main.py` builds the control services, the limiter and the control routes, and protects data routes with the owner or principal dependency. `tests/test_control_plane.py`, `tests/test_agent_facade.py`. |
| R1-03 | P1 | Creating a space failed after storage was partly created, and the identifier stayed reserved. | Fixed | `spaces.py` `provision_new` validates first, refuses existing storage, and removes its own partial storage on failure. `tests/test_space_isolation_and_console.py`. |
| R1-04 | P2 | Access-log route names were recorded as `unmatched` for routes in included routers. | Fixed | `api/access_log.py` reads FastAPI's effective route context. |
| R1-05 | P2 | Anonymous requests each wrote an access row with no limit on the table. | Mitigated | Rows hold metadata only. The table is pruned to 100 000 rows every 200 writes. Anonymous requests are still recorded so failures can be audited. A flood still causes write load. See open item O-02. |
| R1-06 | P2 | The rate limiter pruned expired keys only, so many distinct keys could grow memory without bound. | Fixed | `control/ratelimit.py` caps the tracked keys at 10 000 and drops the least recently used key. |
| R1-07 | P2 | Non-default spaces could share one vector directory when a global override was set. | Fixed | `spaces.py` `settings_for` clears the override for every non-default space. |
| R1-08 | P2 | The control database engine did not set foreign keys, a busy timeout or WAL mode itself. | Fixed in this release | `control/database.py` sets them on every connection. The process-wide listener also sets them for the memory databases. |
| R1-09 | P2 | Readiness was recorded before the seed step committed, so a failed seed was never repeated. | Fixed | `control/database.py` records readiness only after the transaction commits. |
| R1-10 | P2 | A request without a selected space was served from the default space. | Fixed | `api/deps.py` refuses it with `space_not_selected`. |
| R1-11 | P2 | A read-scoped check could allow a write when a write permission was not checked. | Fixed | Write routes require the write permission and `readwrite` on the space (`require_write_space`). |
| R1-12 | P2 | The authentication-failure limit is keyed by the socket address. Behind one reverse proxy, all clients share a bucket. | Open (documented) | See open item O-03. |
| R1-13 | P3 | The token prefix included secret characters. | Fixed | The prefix is the public identifier only. |
| R1-14 | P3 | Key and token files were written before their permissions were set. | Fixed | The control key is created with exclusive creation and mode 0600. The owner token file is created with mode 0600. |
| R1-15 | P3 | A token could be issued without an expiry. | Fixed | The lifetime is required, between 1 and 3650 days. |
| R1-16 | P3 | Rotation reset the lifetime to the default regardless of the original. | Fixed | Rotation keeps the exact original lifetime in seconds. |
| R1-17 | P3 | The documentation said a forget token's digest was cleared on use. The digest is only marked consumed. | Open (consistency) | Replay is refused by status and by the consumed marker. Documentation is accurate in this release. See open item O-06. |
| R1-18 | P3 | CORS did not allow the space header or the PUT method. | Fixed | `main.py` allows `X-JarvisMind-Space`, `PUT`, `PATCH` and `DELETE` for listed origins. |
| R1-19 | P3 | An archived space could be re-activated without its storage being checked. | Fixed | `api/v1/control.py` calls `ensure_provisioned` before a space is re-activated. A dedicated test for this path is still missing (O-07). |

## Round 2: sensitive metadata, agent override, backups and timing

| ID | Severity | Finding | Status | Evidence |
| --- | --- | --- | --- | --- |
| R2-01 | P1 | Sensitive text in metadata reached agents and the default export, because only the content was checked. | Fixed | `control/sensitivity.py` checks metadata keys and string values, and the export and the agent paths use the same rule. `tests/test_security_followups.py`. |
| R2-02 | P1 | An agent could re-issue the owner's pending forget token by sending the same request again, so the owner's approval failed. | Fixed | The agent path never rotates a token. The owner path refuses to replace an agent-pending request (`409`, `agent_request_pending`). `tests/test_security_followups.py`. |
| R2-03 | P2 | Backups were written to the installation's data directory, not to the selected space's directory. | Fixed | `api/v1/console.py` derives the backup directory from the selected space's container. `tests/test_security_followups.py`. |
| R2-04 | P2 | A second backup in the same second had a name that the listing and verification did not accept. | Fixed | Numbered suffixes are accepted by the name pattern. `tests/test_security_followups.py`. |
| R2-05 | P3 | A database comment described the seed as part of the schema transaction. | Fixed | Comment corrected, and readiness is recorded after commit. |
| R2-06 | P3 | An agent's default space could be archived, leaving the agent without a usable space. | Fixed | Archiving is refused while an agent uses the space as its default (`409`, `space_in_use`). `tests/test_security_followups.py`. |
| R2-07 | P3 | A refused agent read still updated the memory's access time. | Fixed | Refused reads do not touch the memory. `tests/test_security_followups.py`. |
| R2-08 | P3 | Token lifetime was computed in whole days, so rotation changed it. | Fixed | Exact seconds. |
| R2-09 | Unverified | The graph filter that hides nodes with invalid sources, and the refusal of merged memories on the agent path, were confirmed by reading the code only. | Open (not exercised) | The release environment ran with the graph disabled. See open item O-08. |

## Round 3: final self-check before the release report

This round was a self-check during the documentation pass. It was not done by a separate reviewer.

| ID | Severity | Finding | Status | Evidence |
| --- | --- | --- | --- | --- |
| R3-01 | P1 | Numbers in metadata were not checked. `{"contact": 13800138000}` was not flagged. | Fixed | `control/sensitivity.py` checks numbers as text. `tests/test_security_followups.py`, `test_metadata_scan_exempts_only_generated_identifiers`. |
| R3-02 | P1 | Values under identifier-named keys were never checked. `{"contact_id": "13800138000"}` was not flagged. | Fixed; the exemption is narrowed to the exact generated shapes (open item O-01) | Same test. |
| R3-03 | P3 | Documentation said the duplicate threshold of `remember` is configurable. It is fixed at 0.82. The variable belongs to the JMR reflection module. | Fixed in the documentation | `docs/CONFIGURATION.md`, `docs/GOVERNANCE.md`. |
| R3-04 | P3 | Documentation named `details.code` for the agent-credential refusal, which uses `details.reason`. | Fixed in the documentation | `docs/API.md`, `docs/AGENT-INTEGRATION.md`. |
| R3-05 | P3 | Documentation gave wrong paths for hybrid search and index rebuild. | Fixed in the documentation | `docs/MEMORY-MANAGEMENT.md`, `docs/BACKUP.md`. |
| R3-06 | P2 | Documentation said the knowledge graph is not switched by an environment variable. `JARVISMIND_JKG_ENABLED` is a kill switch that the server reads. | Fixed in the documentation | `docs/KNOWLEDGE-GRAPH.md`, `docs/CONFIGURATION.md`, `docs/TROUBLESHOOTING.md`. |
| R3-07 | P2 | Graph export returned a server error while the graph was off. | Fixed | `api/v1/console.py`: `export_graph` returns an empty, labelled result. Checked by the console smoke run. |
| R3-08 | P3 | The graph page requested the overview while the graph was off. | Fixed | `console/src/pages/GraphPage.tsx`. |
| R3-09 | P3 | SQLite database files were created with mode 0644 under the default umask. | Fixed | `cli.py` sets umask 077 for `init-db` and `serve`. A fresh data directory shows a 0700 directory and 0600 files. |
| R3-10 | P3 | Documentation said every environment variable is in the configuration reference. Some modules read further variables. | Fixed in the documentation | `docs/CONFIGURATION.md`, section "Internal and advanced variables". |

## Other evidence

- The 67-test release gate passes, and the full suite passes (see the release report).
- The DSH demos (16 checks each, Python and TypeScript) exercise forgetting, recall after forgetting, search after forgetting,
  sensitive holding, credential-free receipts and the non-human correction refusal against a live server.
- Console source contains no `dangerouslySetInnerHTML`, `innerHTML` or `eval`. Its unit tests and production build pass.
- The backend has no file upload endpoints and no WebSocket endpoints.

## The eighteen surfaces

The status of each surface is in [SECURITY.md](SECURITY.md#the-eighteen-review-surfaces). The surfaces that carry an
open item are: API tokens (O-03 and O-06), sensitive data (O-01), backup (O-04) and rate limiting (O-03).

## Open items

| ID | Severity | Item | Mitigation today | Plan |
| --- | --- | --- | --- | --- |
| O-01 | P1, narrowed in round 3 | Values under identifier-named keys (`*_id`, `*_key`, `id`, `uuid`) are checked unless they have the exact shape the system generates: 32 hex digits, a UUID, or a lowercase prefix and 16 hex digits. Any other value under such a key is checked as free text. | Narrowed (R3-02). The residual is a value that has exactly one of the generated shapes and carries sensitive text. The credential gate and the sensitivity rules still apply to everything else. | Confirm the residual risk in the independent audit. Add a test for every new identifier format before the system starts generating it. |
| O-02 | P2 | Anonymous requests are logged and each costs a database write. A flood increases load. | Retention caps the table. The authentication limit answers quickly. | Stop writing rows for anonymous requests, or move the access log to a buffered writer. |
| O-03 | P2 | Authentication-failure and rate limits are per process and keyed by the socket address. Behind a proxy, one bucket is shared. | Limit at the proxy as well. Do not expose the server directly. | Trust a configured proxy's forwarded address, and keep one limit per credential. |
| O-04 | P2 | Backups and exports are plain SQLite and JSON files. | File permissions and the operator's storage controls. | Optional encryption with an operator-held key. |
| O-05 | P2 | The sensitivity policy is a rule set. It is not a complete classifier. | Protected content is held. A rule that does not fire is not proof of safety. | A reviewed rule set with tests for each category, and a feedback path from the owner's decisions. |
| O-06 | P3 | The documentation of the consumed forget token and its stored digest is inconsistent. | Replay is refused. | Align the wording or clear the digest on consumption. |
| O-07 | P3 | No dedicated test re-activates an archived space whose storage is missing. The code path exists (R1-19). | The code path is in place. | Add a test that activates an archived space with missing storage and expects a refusal. |
| O-08 | P2 | The graph's validity filter and the agent refusal of merged memories were reviewed by reading the code only. | Covered by the code review. | Run them with the graph enabled in a test. |
| O-09 | P3 | No dependency vulnerability scan has been run in this release. | None beyond the pinned minimum versions in `requirements.txt`. | Run a scanner in CI before each release. |
| O-10 | P3 | Docker, Windows and Linux deployments were not run. | The configuration is documented and reviewed. | Verify each platform before production use. |

## Next review

Before a production deployment, repeat the review on the final tree with the open items in view, run a dependency scan, and
test the proxy setup you intend to use. Record the results in this document.
