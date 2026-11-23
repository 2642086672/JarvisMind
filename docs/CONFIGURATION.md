# Configuration

JarvisMind reads its settings from environment variables and from the owner token file. There is no
configuration file to edit. A value that cannot be parsed, or that is out of range, stops the server at startup
with a message that names the variable.

Boolean variables accept `1`, `true`, `yes`, `on` (true) and `0`, `false`, `no`, `off` (false). An empty
variable means the default.

## Storage and network

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVISMIND_DATA_DIR` | `data` | Data directory. A relative path is resolved against the directory the server is started from. |
| `JARVISMIND_HOST` | `127.0.0.1` | Bind address. `0.0.0.0`, `::` and `*` also require `JARVISMIND_ALLOW_REMOTE=1`. |
| `JARVISMIND_ALLOW_REMOTE` | `0` | Allows binding to all interfaces. Without it the server refuses such an address. |
| `JARVISMIND_PORT` | `8777` | Listening port, 1 to 65535. |
| `JARVISMIND_ENV` | `development` | Environment label shown in system information. |
| `JARVISMIND_LOG_LEVEL` | `INFO` | Log level. |
| `JARVISMIND_DB_ECHO` | `0` | Log every SQL statement. For debugging only. |
| `JARVISMIND_CORS_ORIGINS` | empty | Comma-separated browser origins allowed to call the API. Empty disables CORS. |
| `JARVISMIND_CONSOLE_DIR` | empty | Directory of the built console. When set, the console is served at `/console/`. |

## Owner credential

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVISMIND_API_TOKEN` | empty | Owner token. When empty, the token in `data/.jarvismind_token` is used, or created on first start. |

Agent tokens are not configured here. They are issued from the console or the owner API, and they are stored only as
HMAC digests in `control.db`, keyed with `data/.control_key`.

## Rate limits

Counters are kept per process in a sliding 60-second window. The response to a refused request is `429` with a
`Retry-After` header. A limit of `0` disables that limit.

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVISMIND_RATE_LIMIT_OWNER_PER_MINUTE` | `600` | Requests per minute for the owner credential. |
| `JARVISMIND_RATE_LIMIT_AGENT_PER_MINUTE` | `120` | Requests per minute for each agent token. |
| `JARVISMIND_AUTH_FAILURE_LIMIT_PER_MINUTE` | `20` | Failed authentication attempts per client-address digest. |

## Governance

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVISMIND_DIRECT_WRITE_ENABLED` | `0` | Enables `POST /api/v1/memories` and `PATCH /api/v1/memories/{id}`. When off, these routes return `direct_write_disabled`. Corrections should use modification requests instead. |
| `JARVISMIND_AUTO_SAVE_ENABLED` | `0` | Reported by the extraction status and governance overview. It does **not** change how explicit `remember` requests are handled: those pass the credential, sensitivity and duplicate checks and are saved when they pass. |
| `JARVISMIND_P1_GOVERNANCE_ENABLED` | `0` | Advanced: enables the unified write gate. Off by default. |
| `JARVISMIND_JMR_DUPLICATE_THRESHOLD` | `0.82` | Similarity from 0 to 1 that the JMR reflection module uses to group duplicate memories into proposals. It does not change the duplicate check of `remember`, which uses a fixed threshold of 0.82 (`core/dedup.py`). |
| `JARVISMIND_LIFECYCLE_IMPORT_ENABLED` | `0` | Enables administrative bulk import through the lifecycle API. |
| `JARVISMIND_LIFECYCLE_MERGE_ENABLED` | `1` | Enables merging two memories into one product. |
| `JARVISMIND_LIFECYCLE_SUMMARY_REVIEW_ENABLED` | `1` | Enables the owner's review of generated summaries. |
| `JARVISMIND_LIFECYCLE_CORRECTION_RESOLVE_ENABLED` | `1` | Enables the owner's resolution of generated corrections. |
| `JARVISMIND_FORGET_TOKEN_TTL_SECONDS` | `300` | Lifetime of a one-time confirmation token for forget and modification requests, 30 to 3600 seconds. |

## Embeddings and search

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVISMIND_EMBEDDING_PROVIDER` | `fastembed` | `fastembed`: semantic vectors from a local model, downloaded once on first use into `data/models`. `disabled`: no vectors, keyword search only. `deterministic`: local n-gram vectors with no model download, for offline use and tests. |
| `JARVISMIND_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | Model name for `fastembed`. |
| `JARVISMIND_EMBEDDING_DIMENSION` | `512` | Vector size, 1 to 8192. Must match the model. |
| `JARVISMIND_WARMUP_EMBEDDING` | `1` | Load the embedding model in a daemon thread once `serve` is listening, so the first recall does not pay for a cold model load inside a request. Only `serve` starts it — tests and `create_app` never do. Set to `0` to disable; an offline machine, a missing cache or a provider that raises is logged once at debug level and ignored. |
| `JARVISMIND_QDRANT_COLLECTION` | `memories` | Collection name in the local vector index. |
| `JARVISMIND_QDRANT_PATH` | empty | Overrides the vector directory of the default space. Other spaces always use their own directory. |
| `JARVISMIND_HYBRID_RRF_K` | `60` | Constant of reciprocal rank fusion, 1 to 1000. |
| `JARVISMIND_HYBRID_VECTOR_WEIGHT` | `1.0` | Weight of the vector ranking in the fusion. |
| `JARVISMIND_HYBRID_FTS_WEIGHT` | `1.0` | Weight of the keyword ranking in the fusion. |
| `JARVISMIND_HYBRID_MAX_CANDIDATES` | `50` | Candidates taken from each ranking, 1 to 500. |
| `JARVISMIND_CONTEXT_MIN_MATCH_RATIO` | `0.4` | Relevance floor for context: the share of the query's topic terms a memory must contain. Lower values inject more, possibly less relevant, memories. |
| `JARVISMIND_CONTEXT_CANDIDATE_LIMIT` | `40` | Candidates examined when building a context block, 10 to 100. |

## Extraction with a language model (optional)

Extraction uses a language model only when both the base URL and the model name are set. Otherwise it uses the
deterministic local engine.

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVISMIND_LLM_PROVIDER` | `openai_compatible` | Provider label. |
| `JARVISMIND_LLM_BASE_URL` | empty | Base URL of an OpenAI-compatible API. |
| `JARVISMIND_LLM_API_KEY` | empty | API key for that API. It is never written to logs or responses. |
| `JARVISMIND_LLM_MODEL` | empty | Model name. |
| `JARVISMIND_LLM_TIMEOUT` | `20` | Request timeout in seconds. |
| `JARVISMIND_LLM_MAX_TOKENS` | `800` | Maximum output tokens, 64 to 8192. |
| `JARVISMIND_LLM_BUDGET_CNY` | `0` | Budget ceiling in CNY used by the extraction budget checks. |
| `JARVISMIND_LLM_PRICE_IN` | `0` | Price per 1000 input tokens, in CNY, for budget accounting. |
| `JARVISMIND_LLM_PRICE_OUT` | `0` | Price per 1000 output tokens, in CNY, for budget accounting. |
| `JARVISMIND_LLM_AUTO_SAVE_THRESHOLD` | `0.85` | Confidence threshold reported in the extraction status. The confidence-based auto-approval path has been removed, so this value approves nothing. |

## Knowledge graph worker (optional)

The worker runs only when `POST /api/v1/jkg/worker/run` is called. Nothing runs in the background. The graph
itself is switched on in the database. See [KNOWLEDGE-GRAPH.md](KNOWLEDGE-GRAPH.md).

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVISMIND_JKG_WORKER_ENABLED` | `0` | Allows the worker run endpoint to execute a pass. |
| `JARVISMIND_JKG_WORKER_LEASE_TTL_SECONDS` | `300` | How long a run holds the lease, 30 to 3600 seconds. |
| `JARVISMIND_JKG_WORKER_BATCH_LIMIT` | `200` | Audit rows processed per run, 1 to 500. |
| `JARVISMIND_JKG_WORKER_RECONCILE_EVERY` | `0` | Run one reconcile page every N runs. `0` disables reconcile. |

## Settings stored in the database

| Key (`system_settings`) | Meaning |
| --- | --- |
| `jkg.enabled` | Switch for the knowledge graph. Values `1`, `true`, `yes` and `on` enable it; `0`, `false`, `no` and `off` disable it, and any other value disables it. A missing row defers to `JARVISMIND_JKG_ENABLED`. |

## Example environment file

```bash
JARVISMIND_DATA_DIR=/var/lib/jarvismind/data
JARVISMIND_HOST=127.0.0.1
JARVISMIND_PORT=8777
JARVISMIND_CONSOLE_DIR=/opt/jarvismind/console/dist
JARVISMIND_EMBEDDING_PROVIDER=disabled
JARVISMIND_LOG_LEVEL=INFO
```

## Optional components

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVISMIND_MCE_ENABLED` | `1` | Process switch for the context engine. `0` turns the MCE routes off. |
| `JARVISMIND_MCE_JKG_ENABLED` | `1` | Lets MCE add knowledge-graph neighbours to context blocks. It has an effect only when the graph is usable. |
| `JARVISMIND_JKG_ENABLED` | empty | Kill switch for the knowledge graph. `0`, `false`, `no` or `off` disables it even when `jkg.enabled` is on. `1`, `true`, `yes` or `on` enables it only when no `jkg.enabled` row exists. |

## Internal and advanced variables

Settings for the JMR, P1 governance, MCE and JKG modules are read from the environment in the files below. Their names and defaults are in those files. The ones that matter for operation are described above. Read the module before you change any other setting.

- `backend/jarvismind/jmr/config.py` and `backend/jarvismind/jmr/scheduler_engine.py`: reflection module and scheduler tuning. The duplicate threshold is described above.
- `backend/jarvismind/jmr/cost_governance.py` and the budget entries in `backend/jarvismind/jmr/config.py`: cost governance for JMR runs. The container wires it in. With no budget set, the layer observes only and marks calls `cost_governance_unconfigured`. With a budget set, a call that would exceed it is blocked with the code `cost_governance_blocked`.
- `backend/jarvismind/governance/config.py`: the unified governance gate, which is off unless `JARVISMIND_P1_GOVERNANCE_ENABLED` is on.
- `backend/jarvismind/mce/config.py`: context-engine limits.
- `backend/jarvismind/jkg/config.py`: graph page size, scan limit and reconcile batch limit. Each one is capped in code.
