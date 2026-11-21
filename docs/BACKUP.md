# Backup and data management

Your memories are in SQLite files and a local vector index. A backup is a consistent copy of those files, taken while the
server runs or while it is stopped. A restore is an offline operation, done by an operator. The console can create and
verify backups. It cannot restore one.

## What is stored where

| Data | Location | Backed up by |
| --- | --- | --- |
| Default space memories, graph and audit log | `data/jarvismind.db` | Console backup of the default space, or `scripts/backup_db.py` |
| Default space vectors | `data/qdrant/` | Not part of the SQLite backup. Rebuild with the index rebuild endpoint (`POST /api/v1/memories/index/rebuild`, owner). |
| Each additional space | `data/spaces/<id>/jarvismind.db` and `qdrant/` | Console backup of that space, or the script with that space's data directory |
| Control plane: spaces, agents, grants, token digests, access log | `data/control.db` | Copy the file while the server is stopped, or with the SQLite backup tool |
| Control key | `data/.control_key` | Copy it with `control.db`. Without it, agent tokens cannot be verified. |
| Owner token | `data/.jarvismind_token` or `JARVISMIND_API_TOKEN` | Keep it in your secret store. It is not part of the backups. |

A backup of a memory database contains memory text in plain form. The backup files are not encrypted by JarvisMind in 1.0.
Store them where you would store the original database.

## Creating a backup from the console

1. Select the memory space in the top bar.
2. Open **备份与导出** and press **创建备份**.
3. The server runs `VACUUM INTO` into that space's `backups` directory. The file is named `jarvismind-YYYYMMDDTHHMMSSZ.db`,
   with a numeric suffix when two backups start in the same second.

The API equivalent is `POST /api/v1/console/backups`. Listing is `GET /api/v1/console/backups`, and verification is
`POST /api/v1/console/backups/verify` with `{"name": "..."}`. Verification checks the file's integrity and counts its
memory rows. It only reads the file. A backup from another space is reported as `not_found`.

Backup names are validated against a fixed pattern, so a request cannot read a file outside the backup directory.

## Creating a backup from the command line

```bash
cd backend
python scripts/backup_db.py --output backups/jme-$(date +%Y%m%d-%H%M%S).db
```

The script reads the data directory from `JARVISMIND_DATA_DIR`, as the server does. It prints the counts it verified.

## Verifying and restoring (offline)

The server must be stopped for a restore. Then:

```bash
cd backend
python scripts/restore_db.py backups/jme-20261121-120000.db --dry-run     # verify only, writes nothing
python scripts/restore_db.py backups/jme-20261121-120000.db                # restores into an empty location
python scripts/restore_db.py backups/jme-20261121-120000.db --force        # replaces the live database
```

- The backup is integrity-checked and its schema is validated before anything is written.
- `--force` first copies the current database to `data/backups` as a safety copy. The script reports where it went.
- To restore an additional space, set `JARVISMIND_DATA_DIR` to that space's directory, for example
  `data/spaces/user-42`, and run the same command.

Restoring a control database is done the same way with `control.db` and the matching `.control_key`. Restore both
together. A control database that does not match its key cannot verify agent tokens.

## Exports

The console **备份与导出** page exports:

| Export | Format | Notes |
| --- | --- | --- |
| Memories | JSON or CSV | At most 5 000 rows. Sensitive text is redacted unless you confirm the risk. |
| Knowledge graph | JSON | At most 1 000 nodes and 1 000 edges. |
| Audit log | JSON | Includes the API access summary, which holds metadata only. |

Each file states whether it contains sensitive text and whether it was truncated. Exports need the owner credential. The
browser downloads them with that credential. A link cannot carry it.

## Restore is not an API operation

By design, no endpoint restores a database, and an agent cannot cause a restore. The console states this on the page.

## Schedules and retention

JarvisMind does not run backups on a schedule in 1.0 and does not prune old backup files. Schedule the script or the
console action with your own tooling, and keep a retention policy that matches your needs.

## Checklist before an upgrade

1. Create a backup of every space you use.
2. Verify each backup from the console, or with `restore_db.py --dry-run`.
3. Stop the server, copy `data/` to a separate disk, and only then upgrade.
