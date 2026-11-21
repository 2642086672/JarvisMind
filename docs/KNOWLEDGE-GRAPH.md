# Knowledge graph (JKG)

The knowledge graph is a view of what the memories say about people, projects and events. It is **derived**: every node
and relation comes from memories, and it follows them. It is never a separate memory store, and it cannot make a memory
true or false. If a source memory is forgotten, the graph stops using what depended on it.

## Model

Entity types: `PERSON`, `PROJECT`, `EVENT`.

Relation types, with the entity types they connect:

| Relation | Subject | Object | Example |
| --- | --- | --- | --- |
| `WORKS_ON` | `PERSON` | `PROJECT` | a person works on a project |
| `ATTENDS` | `PERSON` | `EVENT` | a person attends an event |
| `BELONGS_TO` | `EVENT` | `PROJECT` | an event belongs to a project |
| `KNOWS` | `PERSON` | `PERSON` | two people know each other |

A relation that does not match these pairs is not created.

Each node and edge keeps its sources: the memories that established it. A source is **valid** when its memory is
`ACTIVE` or `ARCHIVED`. It is **invalid** when the memory is `DELETED` or `MERGED`. A node or edge with no valid source is
not returned to agents. Responses report the validity of each source, so a caller can see why something is hidden.

Nodes and edges carry a sensitivity label and redaction flags, set by the graph's governance layer. Names and metadata
that look sensitive are redacted in responses. The graph exports and agent graph responses apply the same rule as memory
reads.

## Enabling the graph

The graph has three parts that must be in place before it returns anything: the tables, the switch and the first build.

### 1. Install the tables

The installer is `scripts/upgrade_to_jkg.py`. It is fail-closed and written to be run by an operator.

```bash
cd backend
python scripts/upgrade_to_jkg.py --db data/jarvismind.db --mode inspect          # read-only report
python scripts/upgrade_to_jkg.py --db data/jarvismind.db --mode install \
  --backup-dir data/backups                                                     # writes the DDL in one transaction
```

- `--mode inspect` is the default and writes nothing.
- `--mode install` takes a verified backup into `--backup-dir` first, then creates the tables in one transaction.
- If the database is inside the source tree's data directory (the default `backend/data`), the installer refuses to
  run unless you also pass `--allow-real-database --approval-ref <reference>`. The reference is a free-text record of
  who approved the change.

Stop the server before you run the installer on a database that the server is using.

### 2. Switch it on

The graph is switched on in the database. The row is `system_settings` with the key
`jkg.enabled`. The values `1`, `true`, `yes` and `on` enable the graph, and `0`, `false`, `no` and `off` disable it; any other value disables it. A missing row leaves the decision to the process variable `JARVISMIND_JKG_ENABLED`, which is off by default. That variable is also a kill switch: `0`, `false`, `no` or `off` turns the graph off even when the row is on.

```bash
sqlite3 data/jarvismind.db \
  "INSERT INTO system_settings (key, value, updated_at) VALUES ('jkg.enabled', '1', datetime('now')) \
   ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = datetime('now');"
```

Switch it off the same way with `'0'`. The tables and rows are kept; only the switch changes.

### 3. Build the first graph

Two ways, both through the API:

- **Initialization tasks** build the graph from existing memories in batches. Preview with `GET /api/v1/jkg/init/preview`,
  create a task with `POST /api/v1/jkg/init/tasks`, and run it with `POST /api/v1/jkg/init/tasks/{task_id}/run`. Pause,
  resume, cancel and retry use the matching `/pause`, `/resume`, `/cancel` and `/retry` actions. Failures are listed at
  `/failures`, and `/retry-failed` retries only those.
- **Lifecycle sync** keeps the graph in step with new memories and with forgetting. `POST /api/v1/jkg/sync/tick` runs one
  pass over the audit log. Its `dry_run=true` option computes the result and writes nothing. `POST /api/v1/jkg/sync/reconcile`
  re-checks source memories in keyset pages. Call it again with the returned cursor while `has_more` is true.

Both are pull-based. Nothing runs in the background unless an operator or a scheduler calls them.

### Worker

`GET /api/v1/jkg/worker/status` reports the worker. `POST /api/v1/jkg/worker/run?limit=&dry_run=` runs one pass. The run
needs `JARVISMIND_JKG_WORKER_ENABLED=1`. When it declines to run, it returns `skipped: true` with a `skip_reason`, and it
does not return an error. A run holds a lease for `JARVISMIND_JKG_WORKER_LEASE_TTL_SECONDS`, so two runs cannot overlap.

## Reading the graph

Owner routes (under `/api/v1/jkg`):

| Route | Purpose |
| --- | --- |
| `GET /status` | Whether the graph is enabled, whether its tables exist, and whether it is usable. Always returns 200. |
| `GET /overview` | Exact counts of nodes, edges and sources. Validity counts are taken from a bounded scan (see limits). |
| `GET /nodes`, `GET /nodes/{node_id}` | Node list with search, and one node with its edges and sources. |
| `GET /nodes/{node_id}/neighbors` | One-hop neighbours. Filters: `direction`, `predicate`, `valid_only`. |
| `GET /nodes/{node_id}/traverse` | A bounded breadth-first walk: `hops` (1 to 3, default 1), `max_nodes` (up to 100, default 50), `max_edges` (up to 1000, default 200). |
| `GET /edges`, `GET /edges/{edge_id}` | Edge list, and one edge with its sources. |
| `GET /sync/status`, `GET /worker/status` | Maintenance state. |

Agent routes (need the `graph` permission): `GET /api/v1/agent/graph/status` and `GET /api/v1/agent/graph/nodes`. The
agent list returns only nodes whose source memory is still valid.

## Limits

- Page sizes are at most 100.
- Validity is decided from a scan of at most 1000 edges. When the scan is cut short, the response says so
  (`truncated`). Counts from `COUNT(*)` are exact; counts of valid and invalid edges are exact only within the scan.
- A traversal reports `truncated` when a cap stopped it. It never hides that.
- The graph is a read model. Editing a node or an edge by hand is not available in 1.0.

## The console Graph page

The page shows the graph status, the overview, a node list with search, a node's neighbourhood (one and two hops) and
its relation table, and a JSON export. When the graph is off, the page says so and shows which tables are missing.

Not available in 1.0, shown on the page as **Not Available**: creating, changing and deleting nodes and relations by hand,
shortest-path queries, clustering, screenshot export, duplicate-node and duplicate-edge reports, and the soft-invalidation
preview.
