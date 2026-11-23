# Installation

This guide installs JarvisMind 1.0.0 from the source tree on one machine. Platform notes are in
[deployment/](deployment/). Container installs are in [DOCKER.md](DOCKER.md).

## Requirements

- **Python 3.13.** This is the version used for the release tests. Other versions are not tested.
- **Node.js 20 or newer with npm**, only to build the console. The release build was produced with Node
  26.13 and npm 11.20.
- Disk space for dependencies, the console build, and the embedding model cache. The embedding model is
  downloaded once, on first semantic search (see [CONFIGURATION.md](CONFIGURATION.md#embeddings-and-search)).
- Network access to install packages and for that one-time model download. Once installed, JarvisMind
  runs locally and makes no outbound calls unless you configure an LLM provider.

## 1. Install the backend

```bash
cd backend
python3.13 -m venv .venv
. .venv/bin/activate            # Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 2. Create the database

```bash
python -m jarvismind init-db
```

This creates, in the data directory (`backend/data` by default):

- `jarvismind.db`: the default memory space, schema version 6;
- `control.db`: spaces, agents, grants, token digests and the access log;
Files created from here on are owner-only (the server sets the process umask to 077). On Windows, rely on the
profile's NTFS permissions instead.

The control key `.control_key` is created later, the first time the server needs it (its first request or its
first issued token). It is created with mode 0600.

`init-db` only creates a **new** database. It refuses to run on a database that already has tables, so it is
safe to run twice. `serve` refuses to start until `init-db` has run.

## 3. Start the server

```bash
python -m jarvismind serve
```

The API listens on `http://127.0.0.1:8777` by default. The full list of settings is in
[CONFIGURATION.md](CONFIGURATION.md).

### Keep the data directory outside the checkout

`JARVISMIND_DATA_DIR` defaults to `backend/data/`. Set it to an absolute path outside the
repository and keep using it for every command — that is what makes the database, the
vectors and the owner token survive a re-clone, an upgrade, or a `git clean`:

```bash
export JARVISMIND_DATA_DIR="$HOME/.jarvismind/data"   # add to ~/.zshrc or ~/.bashrc
```

The same rule applies to `init-db`, `serve`, and any maintenance script: if one command
uses a different directory, it will happily create a second, empty database, and the
console will look as if every memory disappeared. If your console is unexpectedly empty,
check this variable first.

## 4. Find the owner token

The owner token is the credential for the console and for operator routes. It is taken from
`JARVISMIND_API_TOKEN` when that variable is set. Otherwise it is read from `<data directory>/.jarvismind_token`, which is
created on the first start with mode 0600. The server never prints the token.

```bash
token_dir="${JARVISMIND_DATA_DIR:-data}"
cat "$token_dir/.jarvismind_token"                # Linux and macOS
Get-Content "$env:JARVISMIND_DATA_DIR\.jarvismind_token"   # Windows PowerShell
```

Send it either as `X-API-Token: <token>` or as `Authorization: Bearer <token>`.

## 5. Build and open the console

```bash
cd ../console
npm ci
npm run build
cd ../backend
JARVISMIND_CONSOLE_DIR="$(cd ../console && pwd)/dist" python -m jarvismind serve
```

Open `http://127.0.0.1:8777/console/` and sign in with the owner token. The token is kept only in the browser tab's
`sessionStorage`. Closing the tab signs out.

## 6. Verify

```bash
curl -s http://127.0.0.1:8777/health
# {"status":"ok","version":"1.0.0","database_connected":true,"schema_version":6}

curl -s -H "Authorization: Bearer $(cat data/.jarvismind_token)" http://127.0.0.1:8777/api/v1/system/info
```

`/health` needs no credential. It reports only status, version and schema version.

## 7. Run the test suite (optional, for developers)

```bash
cd backend
python -m pytest
```

Run the whole suite. `tests/conftest.py` contains a release gate that counts the release tests and refuses a
partial run, so `pytest tests/test_x.py` will stop with an error. A complete run prints a line like
`[P0-03A GATE RESULT] collected=67 executed=67 passed=67 failed=0`. The full run takes a few minutes.

## Upgrading

- **Code.** Pull or copy the new source, run `pip install -r requirements.txt` again, and restart the server.
  Startup never creates or migrates a database.
- **Databases from earlier JME releases (schema v5).** Upgrade them explicitly before starting the server:
  `python scripts/upgrade_to_v6.py --help` shows the arguments. The upgrader takes an explicit database path, runs
  in one transaction, and leaves the database unchanged when it fails. Take a backup first
  ([BACKUP.md](BACKUP.md)).
- **Control database.** The control schema is checked on start. An unsupported version stops the server without
  changing the file.

## Uninstall

Stop the server. Back up the data directory if you may need it, then delete it. Removing the source tree does not
touch data stored elsewhere.
