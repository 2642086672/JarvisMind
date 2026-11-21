# Changelog

All notable changes to JarvisMind are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-22

First release of the unified memory infrastructure.

### Added

- **Memory engine (JME).** Memory create, read, edit under governance, keyword and hybrid search, archive and restore,
  merge, a two-step forget, and a per-memory audit log. Six memory types and four statuses.
- **Governance (JMR).** Credential refusal before any write, personal-sensitivity holds for the owner, duplicate and
  conflict handling, an approval center for candidates, forget requests and modification requests, and non-human
  actor rules for agents.
- **Knowledge graph (JKG).** A derived graph of people, projects and events, with an installer, a database switch,
  initialization tasks, lifecycle sync, a bounded worker and a reader API.
- **Context engine (MCE).** Hybrid retrieval, a relevance floor, an untrusted-data context block and exclusion counts.
- **Memory spaces.** One database and one vector directory per additional space. Space selection fails closed.
- **Agents and tokens.** Agent identities with eight permissions, per-space grants, hashed tokens with expiry, rotation
  and revocation.
- **API v1.** The agent surface and the owner surface, one error envelope, sliding-window rate limits, a metadata-only
  access log, and a generated API reference.
- **Console.** Dashboard, memory explorer and detail, context lab, knowledge graph, governance center, memory spaces,
  agents, operations, API management, integrations, backups and exports, settings. Light and dark themes. Responsive
  layout.
- **Backups.** Per-space consistent backups with verification, and an offline restore script.
- **SDKs.** Python (`jarvismind-sdk`) and TypeScript (`@jarvismind/sdk`) agent clients.
- **DSH integration.** Pre-chat and post-chat hooks, an agent setup script, and a cross-session demo in Python and
  TypeScript.
- **Deployment files.** Dockerfile, compose file, container entrypoint, and CI workflows for tests, builds and releases.
- **Documentation.** Installation, configuration, deployment for Docker, Windows, Linux and macOS, architecture,
  memory management, governance, knowledge graph, agent integration, API, SDK, DSH tutorial, backup, security, security
  audit, troubleshooting, development guide, roadmap.

### Security

- Owner and agent credentials are checked on the server for every request. Agent tokens are stored only as HMAC digests
  and are shown once.
- Forget approval cannot be claimed by a requester. Agents cannot approve or correct memories.
- Sensitive text in content, summaries and metadata (keys, values and numbers) is withheld from agents and redacted in exports. Values under identifier-named keys are checked unless they have a system-generated shape.
- Access logs hold metadata only, with client addresses as keyed digests.
- Two read-only review rounds by separate reviewer agents were run, followed by a final self-check. Their findings and the fixes are listed in
  [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md), with the open items.

### Known limitations

- The release gate and the full test suite pass in the release environment. Docker, Windows and Linux installs were not run
  there. See the release report.
- Governance rules are configuration values, not editable objects.
- The knowledge graph cannot be edited by hand.
- Backups and exports are not encrypted.
- Rate limits are per process and keyed by the socket address.
- The sensitivity policy is a rule set. See the open items in the security audit.

### Provenance

The memory engine core was carried over from an earlier internal project in a clean-room copy. The release report describes
the copy and the checks made on it.

