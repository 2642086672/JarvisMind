# Linux deployment

> **Status:** these steps are written from the Python and Node requirements and the configuration reference. They were
> **not run on a Linux host** in the release environment. Run the verification commands at the end before you rely on
> them.

## Requirements

- Python 3.13 (from your distribution, from the deadsnakes-style repository of your choice, or from `uv`).
- Node.js 20 or newer, only to build the console.
- A dedicated service user, for example `jarvismind`.

## Install

```bash
sudo useradd --system --create-home --home-dir /var/lib/jarvismind jarvismind
sudo mkdir -p /opt/jarvismind
sudo cp -r backend console /opt/jarvismind/        # or clone the repository there
sudo -u jarvismind python3.13 -m venv /opt/jarvismind/backend/.venv
sudo -u jarvismind /opt/jarvismind/backend/.venv/bin/pip install -r /opt/jarvismind/backend/requirements.txt
(cd /opt/jarvismind/console && npm ci && npm run build)
```

Create the database once, with the same environment the service will use:

```bash
sudo -u jarvismind env JARVISMIND_DATA_DIR=/var/lib/jarvismind/data \
  /opt/jarvismind/backend/.venv/bin/python -m jarvismind init-db
```

## Run as a service

`/etc/systemd/system/jarvismind.service`:

```ini
[Unit]
Description=JarvisMind 1.0 memory server
After=network-online.target
Wants=network-online.target

[Service]
User=jarvismind
Group=jarvismind
WorkingDirectory=/opt/jarvismind/backend
Environment=JARVISMIND_DATA_DIR=/var/lib/jarvismind/data
Environment=JARVISMIND_HOST=127.0.0.1
Environment=JARVISMIND_PORT=8777
Environment=JARVISMIND_CONSOLE_DIR=/opt/jarvismind/console/dist
ExecStart=/opt/jarvismind/backend/.venv/bin/python -m jarvismind serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/jarvismind
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jarvismind
sudo systemctl status jarvismind
journalctl -u jarvismind -f
```

## Put a TLS reverse proxy in front

Keep JarvisMind on `127.0.0.1` and publish only the proxy. Example with nginx:

```nginx
server {
    listen 443 ssl;
    server_name memory.example.internal;
    # ssl_certificate and ssl_certificate_key: your certificate files

    location / {
        proxy_pass http://127.0.0.1:8777;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

JarvisMind does not trust forwarded client-address headers. Its per-address authentication-failure limit therefore
sees the proxy address. Add rate limiting at the proxy as well.

## Permissions

- The data directory must be readable and writable only by the service user: `chmod 700 /var/lib/jarvismind/data`.
- The owner token file and `.control_key` are created with mode 0600 by the server. Do not copy them into
  world-readable locations.

## Verify

```bash
curl -s http://127.0.0.1:8777/health
sudo -u jarvismind /opt/jarvismind/backend/.venv/bin/python -m pytest --version
```

Run the full test suite from the backend directory before deploying a new build ([DEVELOPMENT.md](../DEVELOPMENT.md)).
