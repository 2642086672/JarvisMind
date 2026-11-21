# Docker deployment

> **Status:** the container files are written and reviewed, but the image was **not built or run** in the
> release environment, because Docker was not available there. Build it and run the health check before you rely on
> it. The release report lists this as an open item.

The container runs one JarvisMind process. Data lives in a named volume. Only the API port is published, and only on
the loopback interface by default. SQLite is a file, so no database port exists to expose.

## Files

| File | Purpose |
| --- | --- |
| `Dockerfile` | Two stages: the console is built with Node 20, then the runtime image is built on Python 3.13 slim. |
| `docker/entrypoint.sh` | Runs `init-db` once when the database is missing, then starts `serve`. |
| `docker-compose.yml` | One service, one named volume, a health check, loopback port publishing. |
| `.env.example` | Optional settings. Copy it to `.env` and edit it. |
| `.dockerignore` | Keeps virtual environments, data, logs and local tokens out of the build context. |

## Build and start

```bash
cp .env.example .env            # optional; edit JARVISMIND_API_TOKEN if you want a fixed owner token
docker compose build
docker compose up -d
docker compose ps               # the service should become "healthy"
```

Open `http://127.0.0.1:8777/console/`.

## The owner token in a container

If `JARVISMIND_API_TOKEN` is not set, the token is generated on first start and stored in the volume:

```bash
docker compose exec jarvismind cat /data/.jarvismind_token
```

Do not publish the token. Rotate it by setting `JARVISMIND_API_TOKEN` and restarting, or by replacing the file and
restarting.

## Volumes and persistence

- `jarvismind-data` mounts at `/data`. It holds `jarvismind.db`, `control.db`, `.control_key`, the vector index,
  the embedding model cache, backups and additional spaces.
- Removing the container keeps the data. `docker compose down -v` deletes it. Make a backup first
  ([BACKUP.md](BACKUP.md)).
- The image runs as the non-root user `jarvis` (uid 10001). If you bind-mount a host directory, make it writable by
  that uid.

## Health check

The compose file checks `http://127.0.0.1:8777/health` every 30 seconds from inside the container. The endpoint needs
no credential and reports status, version and schema version. Logs:

```bash
docker compose logs -f jarvismind
```

## Embedding model

The default provider is `fastembed`, which downloads a model on first use. The container needs network access for
that one download. To run without any model download, set `JARVISMIND_EMBEDDING_PROVIDER=disabled` in `.env`. Search
then uses keywords only. See [CONFIGURATION.md](CONFIGURATION.md#embeddings-and-search).

## Upgrading and rolling back

1. Back up the volume (see [BACKUP.md](BACKUP.md)).
2. Pull or copy the new source and run `docker compose build`.
3. Run `docker compose up -d`. The entrypoint does not run `init-db` on an existing database. Databases from older
   releases need `scripts/upgrade_to_v6.py` first.
4. To roll back, start the previous image and restore the backup if the schema changed.

## Security notes

- The compose file publishes `127.0.0.1:8777` only. Put a TLS reverse proxy in front of the service before you expose
  it to a network, and keep `JARVISMIND_ALLOW_REMOTE` unset unless the proxy is on the same host.
- `JARVISMIND_HOST=0.0.0.0` is set inside the container so Docker can forward traffic. It is safe only because the
  published port is bound to the host's loopback interface.
- Secrets belong in `.env` or in your secret manager, not in the image.
