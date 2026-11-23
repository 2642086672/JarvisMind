# macOS deployment

macOS is the environment in which the release tests and the end-to-end demos were run. The steps below are the ones
that were used.

## Requirements

- Python 3.13 (for example from the python.org installer, Homebrew, or `uv python install 3.13`).
- Node.js 20 or newer, only to build the console.

## Install and start

```bash
cd backend
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m jarvismind init-db
python -m jarvismind serve
```

Build the console and serve it:

```bash
(cd ../console && npm ci && npm run build)
JARVISMIND_CONSOLE_DIR="$(cd ../console && pwd)/dist" python -m jarvismind serve
```

Keep the data directory outside the source tree for real use:

```bash
mkdir -p "$HOME/Library/Application Support/JarvisMind"
JARVISMIND_DATA_DIR="$HOME/Library/Application Support/JarvisMind/data" python -m jarvismind init-db
JARVISMIND_DATA_DIR="$HOME/Library/Application Support/JarvisMind/data" python -m jarvismind serve
```

Use the same `JARVISMIND_DATA_DIR` for every command. Otherwise the server starts with an empty, uninitialized
directory and refuses to run.

A simpler alternative that also keeps everything under one directory:

```bash
export JARVISMIND_DATA_DIR="$HOME/.jarvismind/data"
```

That places the database next to `~/.jarvismind/dsh-agent.token`, which the DSH plugin and the
MCP server already read, so a single directory holds both halves of the integration. Whichever
you choose, put the `export` in `~/.zshrc`: a directory that only exists in one terminal session
is how an "empty console" happens — the server creates a second database and the old memories
look gone.

## Keep it running (optional)

A launch agent starts the server at login. Create `~/Library/LaunchAgents/io.jarvismind.server.plist` with the program
arguments `/path/to/backend/.venv/bin/python -m jarvismind serve`, a `WorkingDirectory` of the backend folder, and
`EnvironmentVariables` for `JARVISMIND_DATA_DIR` and `JARVISMIND_CONSOLE_DIR`. Load it with
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.jarvismind.server.plist`.

## Verify

```bash
curl -s http://127.0.0.1:8777/health
```

Backups from the console, and the backup script, write to the data directory. See [BACKUP.md](../BACKUP.md).

## Notes

- The server binds to `127.0.0.1` by default. Keep it that way unless you put a reverse proxy in front of it.
- The owner token is in `<data directory>/.jarvismind_token`. Macs do not share it with other accounts, because the file
  is created with mode 0600.
