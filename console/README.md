# JarvisMind Console

The browser console for JarvisMind: memories, the knowledge graph, governance, spaces, agents, operations, API management,
backups and settings.

The console is a client. It calls the same API as any other client and keeps no memory copy. The owner token is held in
`sessionStorage` for the tab only.

## Commands

```bash
npm ci
npm run typecheck      # tsc --noEmit
npm test               # vitest (jsdom)
npm run build          # typecheck, then a production build into dist/
```

Serve the built console from the backend:

```bash
JARVISMIND_CONSOLE_DIR=$(pwd)/dist python -m jarvismind serve    # run from backend/
```

Open `http://127.0.0.1:8777/console/`.

For development with hot reload, run the backend on port 8777, then:

```bash
JARVISMIND_API_URL=http://127.0.0.1:8777 npm run dev
```

## Layout

| Path | Contents |
| --- | --- |
| `src/app/` | Application shell, routing, the console context |
| `src/components/` | Layout, the shared UI kit, error boundary |
| `src/lib/api/` | Typed API client, endpoint table, response types |
| `src/lib/` | Session storage, formatting, hooks, pending confirmation tokens |
| `src/pages/` | One file per page |
| `src/styles/global.css` | Design tokens for the dark and light themes, responsive rules |

## Rules for this code

- Every number on a page comes from the API. A figure the API does not provide is shown as **Not Available** with the
  reason. Nothing is estimated.
- Confirmation tokens for forget and modification requests are kept in memory only, shown once, and never stored.
- Memory text is displayed as text. The console does not use `dangerouslySetInnerHTML`.
- The console never decides a permission. Hiding a button is not a control.
