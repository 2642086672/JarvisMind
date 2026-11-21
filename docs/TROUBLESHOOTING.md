# Troubleshooting

Start with the health check. It needs no credential:

```bash
curl -s http://127.0.0.1:8777/health
```

A healthy server reports `"status":"ok"` and `"database_connected":true`. If the request is refused, the server is not
running or is bound to another address.

## The server will not start

| Message | Cause | Fix |
| --- | --- | --- |
| `JarvisMind is not initialized. Run python -m jarvismind init-db first.` | The data directory has no `jarvismind.db`. | Run `python -m jarvismind init-db` with the same `JARVISMIND_DATA_DIR`. |
| `JARVISMIND_HOST binds to all interfaces but JARVISMIND_ALLOW_REMOTE is not enabled` | `JARVISMIND_HOST` is `0.0.0.0`, `::` or `*`. | Keep `127.0.0.1`, or set `JARVISMIND_ALLOW_REMOTE=1` and put a proxy in front. |
| `... must be an integer ...` or `... must be between ...` | A variable is out of range or malformed. | Check [CONFIGURATION.md](CONFIGURATION.md). The message names the variable. |
| `init-db refused: ...` | The database already exists. This is the protection working. | Use the existing database. Do not delete it to make `init-db` succeed. |
| `unsupported control schema version` | `control.db` was made by an incompatible version. | Restore a matching backup of `control.db` and `.control_key`. |
| `the storage of this memory space exists but is not initialized` | A space directory exists without a database. | Repair it offline, or move the directory aside and provision the space again. |

## Authentication and access

| Symptom | Cause | Fix |
| --- | --- | --- |
| `401 unauthorized` on every call | The token is wrong, or it came from a different data directory. | Read the owner token from the data directory the server uses. Check for trailing spaces. |
| `401` for an agent token | The token is expired, revoked, or its agent is disabled. | Check the agent in the console. Rotate or issue a new token. |
| `403 owner_only` | An agent token was used on an owner route. | Use the `/api/v1/agent/*` routes, or use the owner credential for operator tasks. |
| `403 space_access_denied` | The agent has no grant for the space in `X-JarvisMind-Space`. | Grant the space in the agent's detail page, or remove the header to use the default space. |
| `403 permission_denied` | The agent lacks a permission. | Add the permission to the agent, then check that the token's scopes include it. |
| `429` with `Retry-After` | The rate limit was reached. | Wait the given seconds. Lower the request rate, or raise the limit in configuration. |

## Memory behaviour

| Symptom | Explanation |
| --- | --- |
| A statement returned `PROTECTED` and is not in the list | Sensitive content is held for the owner. Approve it in the approval center to keep it. |
| A statement returned `DENIED` | The text contained a credential. Nothing was stored. Remove the credential from the text. |
| A statement returned `NO_MEMORY` or `NO_INTENT` | The text was not an explicit fact. Rephrase it as a plain statement. |
| A statement returned `DUPLICATE` | A near-identical memory already exists. |
| A recall returns nothing | Check the query terms, the space, the agent's `search` and `context` permissions, and whether the memories are `ACTIVE`. |
| A forgotten memory still appears in the owner console | Forgetting becomes effective only after the owner approves the request (`EXECUTED`). Check the request's status. |
| `JARVISMIND_AUTO_SAVE_ENABLED` has no effect | It is a status flag. It does not gate explicit remember requests. See [CONFIGURATION.md](CONFIGURATION.md#governance). |

## Semantic search

| Symptom | Cause | Fix |
| --- | --- | --- |
| `degraded_to_fts: true` in recall results | Vectors are unavailable, so only keyword search ran. | Check `GET /api/v1/embedding/status`. Check network access for the first model download. |
| The first semantic search is slow or fails | The embedding model is downloaded on first use into `data/models`. | Allow one download, or copy the model cache into `data/models`. |
| You do not want model downloads at all | Expected in offline installs. | Set `JARVISMIND_EMBEDDING_PROVIDER=disabled` (keyword only) or `deterministic` (local vectors, no model). |

## The knowledge graph

| Symptom | Cause | Fix |
| --- | --- | --- |
| The graph page says the graph is disabled | `system_settings['jkg.enabled']` is missing or not truthy, or `JARVISMIND_JKG_ENABLED` is set to `0`, `false`, `no` or `off`. | Switch it on as described in [KNOWLEDGE-GRAPH.md](KNOWLEDGE-GRAPH.md#enabling-the-graph). |
| The graph page says the schema is not ready | The `jkg_*` tables are missing. | Run `scripts/upgrade_to_jkg.py --mode install`. |
| The graph is empty after enabling | The first build has not run. | Create and run an initialization task, or run a sync tick. |
| The worker run returns `skipped` | The worker is disabled, or another run holds the lease. | Set `JARVISMIND_JKG_WORKER_ENABLED=1`, or wait for the lease to expire. |

## The console

| Symptom | Cause | Fix |
| --- | --- | --- |
| `/console/` returns 404 | `JARVISMIND_CONSOLE_DIR` is not set or does not point to a built console. | Run `npm run build` in `console/` and set the variable to its `dist` directory. |
| A blank page, with errors about scripts | The build and the server are from different versions. | Rebuild the console and restart the server. |
| Every page shows "无法连接 JarvisMind API" | The API is not reachable from the browser. | Open the console from the same server address, or add the console origin to `JARVISMIND_CORS_ORIGINS`. |
| The sign-in is refused | Wrong token, or the server runs with a different data directory. | Use the token from the data directory the server is using. |
| The space selector shows only `default` | The owner has not created other spaces, or they are archived. | Create the space in **记忆空间**, or show archived spaces. |
| "Not Available" badges | The 1.0 release does not provide that data. | See [ROADMAP.md](ROADMAP.md). |

## Database

| Symptom | Cause | Fix |
| --- | --- | --- |
| `database is locked` | Two writers collided for more than five seconds. | Stop the second process. Only one server should use a data directory. |
| The database files grow large | WAL mode keeps recent changes in `-wal` files until checkpoint. | Stop the server, or wait. Do not delete the `-wal` file while the server runs. |
| A backup verifies as not `ok` | The file is damaged or is not a JarvisMind backup. | Use the previous backup. Investigate the cause before you restore anything. |

## Agent tokens after restoring data

Agent tokens work only with the `.control_key` that was present when they were issued. A restored `control.db` without its
matching `.control_key` rejects every agent token. Restore both together, or issue new tokens.

## Still stuck?

Collect the server log at `INFO` level, the health output, the versions (`python -m jarvismind version`), and the exact
error envelope. Remove tokens, memory text and personal details before you share them. Report the problem as described in
[CONTRIBUTING.md](../CONTRIBUTING.md).
