# JarvisMind documentation

Start with the [README](../README.md). The documents below describe the 1.0.0 release.

## Getting started

- [Installation](INSTALLATION.md): requirements, first run, upgrades.
- [Configuration](CONFIGURATION.md): the settings operators change, with their defaults and meanings. Internal tuning variables are listed at the end of that page.
- [Docker deployment](DOCKER.md): the image, the compose file, volumes, health checks.
- [Windows deployment](deployment/WINDOWS.md)
- [Linux deployment](deployment/LINUX.md)
- [macOS deployment](deployment/MACOS.md)
- [Troubleshooting](TROUBLESHOOTING.md)

## Concepts

- [Architecture](ARCHITECTURE.md): components, data layout, write and forget paths, limits.
- [Memory management](MEMORY-MANAGEMENT.md): memory types, statuses, editing, forgetting, export.
- [Knowledge graph](KNOWLEDGE-GRAPH.md): the derived graph, enabling it, its API and limits.
- [Governance](GOVERNANCE.md): the credential gate, sensitivity, approval center, audit.

## Integration

- [API v1](API.md): authentication, envelopes, errors, rate limits, the agent surface.
- [API reference (generated)](API-REFERENCE.md): every route, generated from the running server's OpenAPI document.
- [SDK](SDK.md): the Python and TypeScript clients.
- [Agent integration](AGENT-INTEGRATION.md): agents, permissions, spaces, the context block, adapters.
- [DSH integration tutorial](tutorials/INTEGRATE-WITH-DSH.md): the full walk-through with a runnable demo.

## Operations and security

- [Backup and data management](BACKUP.md): console backups, restore procedure, exports.
- [Security](SECURITY.md): the security model, the 18 review surfaces, the release rules.
- [Security audit](SECURITY-AUDIT.md): what was reviewed, what was found, what was fixed, what remains.

## Development

- [Development guide](DEVELOPMENT.md): repository layout, tests, the release gate, adding routes.
- [Roadmap](ROADMAP.md): what 1.0 does not cover, and what comes next.
- [Contributing](../CONTRIBUTING.md), [Code of conduct](../CODE_OF_CONDUCT.md), [Security policy](../SECURITY.md)
- [Changelog](../CHANGELOG.md), [Release report](../RELEASE-1.0-FINAL-REPORT.md)
