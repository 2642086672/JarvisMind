# Roadmap

## Version 1.0.0 (this release)

Included:

- The memory engine (JME) with six memory types, four statuses, search, editing under governance, archive and restore,
  merge, forgetting and an audit log.
- Governance (JMR): the credential gate, personal-sensitivity holds, duplicate and conflict handling, the approval center
  for candidates, forget requests and modification requests.
- The knowledge graph (JKG) as a derived view, with its installer, switch, initialization tasks, sync and reader API.
- The context engine (MCE): hybrid retrieval, a relevance floor, the untrusted-data context block and exclusion counts.
- Memory spaces with one database and one vector directory per additional space.
- Agents with eight permissions, per-space grants and hashed, expiring, rotatable tokens.
- The console: dashboard, memory explorer and detail, context lab, graph, governance center, spaces, agents, operations,
  API management, integrations, backups and exports, settings.
- The API v1: the agent surface and the owner surface, one error envelope, rate limits and an access log.
- Python and TypeScript SDKs, and the DSH integration example with a cross-session demo.
- Documentation for installation, configuration, deployment, architecture, governance, the knowledge graph, agents, the API,
  the SDKs, backups, security and development.

Not included, or included with limits:

- Governance rules as editable, versioned objects. Policies are configuration in 1.0.
- Manual editing of the knowledge graph, shortest-path queries and graph clustering.
- Memory classification editing, tags, source and time-range filters in the console, and bulk operations.
- Multiple owner accounts. One owner credential administers an installation.
- Encryption of backups and exports.
- Verified Docker, Windows and Linux deployments in the release environment.
- Verified integrations for any application other than the DSH example.
- Operations dashboards for queues, dead-letter queues and index health beyond the component checks.

## Planned for 1.1

These are the next items in priority order. None of them is a promise of a date.

1. Verify the Docker image, Windows and Linux installs on real hosts, and publish the results.
2. Close the open items in [SECURITY-AUDIT.md](SECURITY-AUDIT.md): the metadata identifier rule (O-01), anonymous access
   logging (O-02), and proxy-aware rate limits (O-03).
3. Governance rule objects: list, edit, version, enable, disable, test and record executions.
4. Encrypted backups with an operator-held key, and scheduled backups with retention.
5. Verified adapters for DeepSeek, OpenAI-compatible APIs, Claude, LangChain and LlamaIndex, each with its own test.
6. Memory tags, source filters and time-range filters in the console.

## Later

- Multiple owner accounts with roles.
- Manual graph editing with the same governance as memories.
- Metrics export for operations.
- An English console language.
- A hosted option for teams, after the security review is repeated on that deployment.
