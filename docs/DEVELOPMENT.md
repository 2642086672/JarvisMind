# Development guide

This guide is for people who change JarvisMind. For running it, see [INSTALLATION.md](INSTALLATION.md).

## Repository layout

```text
backend/          Python package jarvismind (FastAPI + SQLAlchemy + SQLite), tests, operator scripts
console/          Browser console (React, TypeScript, Vite)
sdk/python/       Python agent client (jarvismind_sdk)
sdk/typescript/   TypeScript agent client (@jarvismind/sdk)
examples/         DSH integration: hooks, setup script, demos
plugins/          DSH Desktop Cordis plugins (dsh-jarvismind: native recall + capture)
docs/             Documentation (this folder)
docker/           Container entrypoint
.github/          CI workflows and templates
```

Inside `backend/jarvismind/`:

| Package or module | Contents |
| --- | --- |
| `api/` | FastAPI routers. `api/v1/` holds the agent, control, console and lab surfaces. `api/auth.py` holds authentication. |
| `control/` | Spaces, agents, grants, tokens, digests, access log, rate limiter, sensitivity policy. |
| `database/` | Connection setup, migrations, bootstrap, backup. |
| `repositories/`, `services/` | Data access and business logic for memories, candidates, forget and modification requests. |
| `jkg/`, `mce/`, `jmr/` | Knowledge graph, context engine and governance cost/policy modules. |
| `core/` | Shared errors, security gate, timestamps, dedup. |
| `config.py`, `container.py`, `spaces.py`, `main.py`, `cli.py` | Settings, wiring, per-space containers, the application factory, the command line. |

## Set up

```bash
cd backend
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cd ../console
npm ci

cd ../sdk/typescript
npm ci
```

## Backend tests

Run the whole suite from `backend/`:

```bash
python -m pytest
```

The suite contains a release gate in `tests/conftest.py`. It counts the gate tests (67 in 1.0.0), requires all of them to be
collected, and reports a line such as `[P0-03A GATE RESULT] collected=67 executed=67 passed=67 failed=0`. A partial run
stops with an error. Always run the full suite before you merge.

The suite starts applications with `TestClient` and temporary data directories.

## Console checks

From `console/`:

```bash
npm run typecheck     # tsc --noEmit
npm test              # vitest, jsdom
npm run build         # typecheck, then a production build into dist/
```

For development with hot reload, run the backend on port 8777 and start Vite:

```bash
JARVISMIND_API_URL=http://127.0.0.1:8777 npm run dev
```

Vite proxies `/api` and `/health` to the backend. Open `http://127.0.0.1:5173/console/`.

## SDK checks

```bash
cd sdk/python && python -m pytest tests
cd sdk/typescript && npm test          # builds, then runs node:test
```

## The end-to-end demo

With a server running and an agent token issued (see [tutorials/INTEGRATE-WITH-DSH.md](tutorials/INTEGRATE-WITH-DSH.md)):

```bash
python examples/dsh-integration/python/demo_cross_session.py
node examples/dsh-integration/typescript/demo.mjs     # after building the TypeScript SDK
```

Both print one line per check and exit with status 0 only when all checks pass.

## Regenerate the API reference

The reference is generated from the running server's OpenAPI document:

```bash
cd backend
python scripts/generate_api_reference.py --base-url http://127.0.0.1:8777 --out ../docs/API-REFERENCE.md
```

Regenerate it whenever a route changes, and keep [API.md](API.md) in step by hand.

## Adding a route

- **Owner route** (console or operator): use the owner dependency, `owner_auth`, on the router or the route. Put it under the
  `/api/v1/console` or `/api/v1/control` prefix when it belongs to the console. Return the standard envelope.
- **Agent route**: use `principal_auth` with `require(<permission>)`. Write routes also need `require_write_space`. Never
  return a confirmation token to an agent. Never call an owner-only service from an agent route.
- **Errors**: raise the classes in `core/errors.py` or `control/errors.py`. Use a stable `details.code` for the reason.
- **Access log**: nothing to do. The middleware records the route template.
- **Tests**: add a test for the success path, the refusal path and the other principal (an owner test for an agent route,
  and an agent test for an owner route).

## Database changes

The schema is at version 6. A change that creates or alters tables needs:

1. a migration with a version number;
2. a test for an empty database and for an existing one;
3. a note in [CHANGELOG.md](../CHANGELOG.md);
4. an upgrade path for databases already in the field, written as an explicit script in `backend/scripts/`.

Do not add DDL to request handlers. Startup must never create or migrate a database.

## Conventions

- Python: type hints on public functions, Pydantic models for request bodies with `extra="forbid"`, no bare `except`.
- TypeScript: strict mode; no `any` in the public SDK types; display text is not HTML.
- Comments explain *why*. A comment that repeats the code is removed.
- Names in the public API are English. User-facing console text is Chinese, with English labels where the spec asks for them.

## Releases

1. Update `CHANGELOG.md` and the version in `backend/jarvismind/config.py`, `console/package.json`, the SDK `package.json`
   and `pyproject.toml`.
2. Run the backend suite, the console checks, the SDK tests and the demos against a live server.
3. Regenerate the API reference.
4. Tag `vX.Y.Z` on the commit that passed. The release workflow builds the artefacts.

## Commits

- Keep commits focused. A commit must pass the full suite.
- Write the subject in the imperative, and say what changed and why in the body.
- Do not commit data directories, tokens, backups, logs or virtual environments. The `.gitignore` covers them.
