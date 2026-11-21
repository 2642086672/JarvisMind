# API reference (generated)

Generated from the OpenAPI document served by JarvisMind 1.0.0 (`GET /openapi.json`).
It lists every route the server exposes under `/api/v1` plus `/health`.
The authentication rules, permissions and examples are in [API.md](API.md).

Total operations: **174**.

## agent

| Method | Path | Summary |
| --- | --- | --- |
| `POST` | `/api/v1/agent/context` | Context |
| `GET` | `/api/v1/agent/events` | My Events |
| `POST` | `/api/v1/agent/extract` | Extract |
| `GET` | `/api/v1/agent/governance/requests` | My Requests |
| `GET` | `/api/v1/agent/graph/nodes` | Graph Nodes |
| `GET` | `/api/v1/agent/graph/status` | Graph Status |
| `GET` | `/api/v1/agent/me` | Me |
| `GET` | `/api/v1/agent/memories` | List Memories |
| `GET` | `/api/v1/agent/memories/{memory_id}` | Get Memory |
| `POST` | `/api/v1/agent/memories/{memory_id}/forget-requests` | Request Forget |
| `POST` | `/api/v1/agent/memories/{memory_id}/modification-requests` | Request Modification |
| `POST` | `/api/v1/agent/recall` | Recall |
| `POST` | `/api/v1/agent/remember` | Remember |
| `GET` | `/api/v1/agent/search` | Search |

## console

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/console/activity` | Activity |
| `GET` | `/api/v1/console/api-management` | Api Management |
| `GET` | `/api/v1/console/backups` | List Backups |
| `POST` | `/api/v1/console/backups` | Create Backup |
| `POST` | `/api/v1/console/backups/verify` | Verify Backup |
| `GET` | `/api/v1/console/dashboard` | Dashboard |
| `GET` | `/api/v1/console/exports/audit` | Export Audit |
| `GET` | `/api/v1/console/exports/graph` | Export Graph |
| `GET` | `/api/v1/console/exports/memories` | Export Memories |
| `GET` | `/api/v1/console/governance/overview` | Governance Overview |
| `POST` | `/api/v1/console/lab/context` | Lab Context |
| `GET` | `/api/v1/console/ops/components` | Ops Components |
| `GET` | `/api/v1/console/ops/logs` | Ops Logs |
| `GET` | `/api/v1/console/ops/tasks` | Ops Tasks |
| `GET` | `/api/v1/console/settings` | Safe Settings |

## control

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/control/access-logs` | List Access Logs |
| `GET` | `/api/v1/control/access-logs/summary` | Access Log Summary |
| `GET` | `/api/v1/control/agents` | List Agents |
| `POST` | `/api/v1/control/agents` | Create Agent |
| `GET` | `/api/v1/control/agents/{agent_id}` | Get Agent |
| `PATCH` | `/api/v1/control/agents/{agent_id}` | Update Agent |
| `DELETE` | `/api/v1/control/agents/{agent_id}` | Delete Agent |
| `PUT` | `/api/v1/control/agents/{agent_id}/grants/{space_id}` | Set Grant |
| `DELETE` | `/api/v1/control/agents/{agent_id}/grants/{space_id}` | Revoke Grant |
| `POST` | `/api/v1/control/agents/{agent_id}/tokens` | Issue Token |
| `GET` | `/api/v1/control/permissions` | List Permissions |
| `GET` | `/api/v1/control/spaces` | List Spaces |
| `POST` | `/api/v1/control/spaces` | Create Space |
| `GET` | `/api/v1/control/spaces/{space_id}` | Get Space |
| `PATCH` | `/api/v1/control/spaces/{space_id}` | Update Space |
| `GET` | `/api/v1/control/tokens` | List Tokens |
| `POST` | `/api/v1/control/tokens/{token_id}/revoke` | Revoke Token |
| `POST` | `/api/v1/control/tokens/{token_id}/rotate` | Rotate Token |

## embedding

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/embedding/status` | Get Embedding Status |

## extraction

| Method | Path | Summary |
| --- | --- | --- |
| `POST` | `/api/v1/memories/approval/recover` | Recover Unknown Approvals |
| `POST` | `/api/v1/memories/auto-govern/expire` | Auto Govern Expire |
| `POST` | `/api/v1/memories/auto-govern/merge` | Auto Govern Merge |
| `GET` | `/api/v1/memories/auto-govern/summary` | Auto Govern Summary |
| `POST` | `/api/v1/memories/auto-remember` | Auto Remember |
| `GET` | `/api/v1/memories/block-events` | List Block Events |
| `GET` | `/api/v1/memories/candidates` | List Candidates |
| `GET` | `/api/v1/memories/candidates/{candidate_id}` | Get Candidate |
| `POST` | `/api/v1/memories/candidates/{candidate_id}/approve` | Approve Candidate |
| `POST` | `/api/v1/memories/candidates/{candidate_id}/reject` | Reject Candidate |
| `POST` | `/api/v1/memories/extract` | Extract Memories |
| `GET` | `/api/v1/memories/extraction-events` | List Extraction Events |
| `GET` | `/api/v1/memories/extraction/stats` | Extraction Stats |
| `GET` | `/api/v1/memories/extraction/status` | Extraction Status |

## health

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/health` | Health |

## hybrid-search

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/memories/hybrid-search` | Hybrid Search |
| `GET` | `/api/v1/memories/index/consistency` | Check Consistency |
| `POST` | `/api/v1/memories/index/rebuild` | Rebuild Index |
| `POST` | `/api/v1/memories/index/reconcile` | Reconcile Index |
| `GET` | `/api/v1/memories/index/status` | Get Index Status |

## jkg

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/jkg/edges` | List Edges |
| `GET` | `/api/v1/jkg/edges/{edge_id}` | Get Edge |
| `GET` | `/api/v1/jkg/extraction/preview/{memory_id}` | Get Extraction Preview |
| `GET` | `/api/v1/jkg/init/preview` | Get Init Preview |
| `POST` | `/api/v1/jkg/init/tasks` | Create Init Task |
| `GET` | `/api/v1/jkg/init/tasks` | List Init Tasks |
| `GET` | `/api/v1/jkg/init/tasks/{task_id}` | Get Init Task |
| `POST` | `/api/v1/jkg/init/tasks/{task_id}/cancel` | Cancel Init Task |
| `GET` | `/api/v1/jkg/init/tasks/{task_id}/failures` | List Init Failures |
| `POST` | `/api/v1/jkg/init/tasks/{task_id}/pause` | Pause Init Task |
| `POST` | `/api/v1/jkg/init/tasks/{task_id}/resume` | Resume Init Task |
| `POST` | `/api/v1/jkg/init/tasks/{task_id}/retry` | Retry Init Task |
| `POST` | `/api/v1/jkg/init/tasks/{task_id}/retry-failed` | Retry Failed Init Task |
| `POST` | `/api/v1/jkg/init/tasks/{task_id}/run` | Run Init Task |
| `GET` | `/api/v1/jkg/nodes` | List Nodes |
| `GET` | `/api/v1/jkg/nodes/{node_id}` | Get Node |
| `GET` | `/api/v1/jkg/nodes/{node_id}/neighbors` | List Neighbors |
| `GET` | `/api/v1/jkg/nodes/{node_id}/traverse` | Traverse Node |
| `GET` | `/api/v1/jkg/overview` | Get Overview |
| `GET` | `/api/v1/jkg/status` | Get Status |
| `POST` | `/api/v1/jkg/sync/reconcile` | Post Sync Reconcile |
| `GET` | `/api/v1/jkg/sync/status` | Get Sync Status |
| `POST` | `/api/v1/jkg/sync/tick` | Post Sync Tick |
| `POST` | `/api/v1/jkg/worker/run` | Post Worker Run |
| `GET` | `/api/v1/jkg/worker/status` | Get Worker Status |

## jmr

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/jmr/auto-execute` | Get Auto Execute |
| `POST` | `/api/v1/jmr/auto-execute/disable` | Disable Auto Execute |
| `POST` | `/api/v1/jmr/auto-execute/enable` | Enable Auto Execute |
| `POST` | `/api/v1/jmr/auto-execute/run` | Run Auto Execute |
| `GET` | `/api/v1/jmr/conflicts` | List Conflicts |
| `GET` | `/api/v1/jmr/cost` | Get Cost Report |
| `GET` | `/api/v1/jmr/proposals` | List Proposals |
| `GET` | `/api/v1/jmr/proposals/{proposal_id}` | Get Proposal |
| `POST` | `/api/v1/jmr/proposals/{proposal_id}/review` | Review Proposal |
| `POST` | `/api/v1/jmr/runs` | Start Run |
| `GET` | `/api/v1/jmr/runs/{task_id}` | Get Run |
| `POST` | `/api/v1/jmr/runs/{task_id}/cancel` | Cancel Run |
| `POST` | `/api/v1/jmr/runs/{task_id}/pause` | Pause Run |
| `POST` | `/api/v1/jmr/runs/{task_id}/resume` | Resume Run |
| `GET` | `/api/v1/jmr/scheduler` | Get Scheduler Status |
| `POST` | `/api/v1/jmr/scheduler/tick` | Run Scheduler Tick |
| `GET` | `/api/v1/jmr/status` | Get Status |

## mce

| Method | Path | Summary |
| --- | --- | --- |
| `POST` | `/api/v1/mce/resolve` | Resolve Project Context |
| `GET` | `/api/v1/mce/status` | Mce Status |

## memories

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/memories` | List Memories |
| `POST` | `/api/v1/memories` | Create Memory |
| `GET` | `/api/v1/memories/search` | Search Memories |
| `GET` | `/api/v1/memories/{memory_id}` | Get Memory |
| `PATCH` | `/api/v1/memories/{memory_id}` | Update Memory |
| `DELETE` | `/api/v1/memories/{memory_id}` | Delete Memory |
| `GET` | `/api/v1/memories/{memory_id}/audit-logs` | Memory Audit Logs |

## memory-context

| Method | Path | Summary |
| --- | --- | --- |
| `POST` | `/api/v1/memories/context` | Retrieve Memory Context |

## memory-forget

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/forget-requests` | List Forget Requests |
| `GET` | `/api/v1/forget-requests/{request_id}` | Get Forget Request |
| `POST` | `/api/v1/forget-requests/{request_id}/approve` | Approve Forget Request |
| `POST` | `/api/v1/forget-requests/{request_id}/reject` | Reject Forget Request |
| `POST` | `/api/v1/memories/{memory_id}/forget-requests` | Create Forget Request |

## memory-lifecycle

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/memories/archive-records` | List Archive Records |
| `GET` | `/api/v1/memories/corrections` | List Corrections |
| `POST` | `/api/v1/memories/corrections/detect` | Detect Corrections |
| `GET` | `/api/v1/memories/corrections/{correction_id}` | Get Correction |
| `POST` | `/api/v1/memories/corrections/{correction_id}/reject` | Reject Correction |
| `POST` | `/api/v1/memories/corrections/{correction_id}/resolve` | Resolve Correction |
| `GET` | `/api/v1/memories/expired` | List Expired |
| `GET` | `/api/v1/memories/expiring` | List Expiring |
| `GET` | `/api/v1/memories/export` | Export Memories |
| `POST` | `/api/v1/memories/forget` | Forget Memories |
| `GET` | `/api/v1/memories/forget-candidates` | Forget Candidates |
| `GET` | `/api/v1/memories/forget-preview/{memory_id}` | Forget Preview |
| `POST` | `/api/v1/memories/import` | Import Memories |
| `POST` | `/api/v1/memories/import/preview` | Preview Import |
| `GET` | `/api/v1/memories/merges` | List Merges |
| `POST` | `/api/v1/memories/merges/confirm` | Confirm Merge |
| `POST` | `/api/v1/memories/merges/preview` | Preview Merge |
| `GET` | `/api/v1/memories/merges/{merge_id}` | Get Merge |
| `POST` | `/api/v1/memories/merges/{merge_id}/reject` | Reject Merge |
| `POST` | `/api/v1/memories/summaries` | Create Summary |
| `GET` | `/api/v1/memories/summaries` | List Summaries |
| `GET` | `/api/v1/memories/summaries/{summary_id}` | Get Summary |
| `POST` | `/api/v1/memories/summaries/{summary_id}/review` | Review Summary |
| `POST` | `/api/v1/memories/{memory_id}/archive` | Archive Memory |
| `POST` | `/api/v1/memories/{memory_id}/expiry-review` | Review Expiry |
| `POST` | `/api/v1/memories/{memory_id}/restore` | Restore Memory |
| `GET` | `/api/v1/memories/{memory_id}/reviews` | List Reviews |

## memory-modification

| Method | Path | Summary |
| --- | --- | --- |
| `POST` | `/api/v1/memories/{memory_id}/modification-requests` | Create Modification Request |
| `GET` | `/api/v1/memories/{memory_id}/modification-requests` | List Memory Modification Requests |
| `GET` | `/api/v1/modification-requests` | List Modification Requests |
| `GET` | `/api/v1/modification-requests/{request_id}` | Get Modification Request |
| `POST` | `/api/v1/modification-requests/{request_id}/approve` | Approve Modification Request |
| `POST` | `/api/v1/modification-requests/{request_id}/reject` | Reject Modification Request |

## memory-overview

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/memories/overview` | Get Memory Overview |

## memory-sources

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/memory-sources` | List Sources |
| `POST` | `/api/v1/memory-sources` | Create Source |
| `GET` | `/api/v1/memory-sources/{source_id}` | Get Source |
| `PATCH` | `/api/v1/memory-sources/{source_id}` | Update Source |
| `DELETE` | `/api/v1/memory-sources/{source_id}` | Delete Source |

## system

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/system/info` | System Info |

## untagged

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/v1/p1/audit` | Audit Entries |
| `POST` | `/api/v1/p1/memories` | Governed Create Memory |
| `GET` | `/api/v1/p1/memories` | Projection List |
| `GET` | `/api/v1/p1/status` | Governance Status |
| `POST` | `/api/v1/p1/tasks` | Create Task |
| `GET` | `/api/v1/p1/tasks/{task_id}` | Get Task |
| `POST` | `/api/v1/p1/tasks/{task_id}/cancel` | Cancel Task |
| `POST` | `/api/v1/p1/tasks/{task_id}/pause` | Pause Task |
| `POST` | `/api/v1/p1/tasks/{task_id}/resume` | Resume Task |
