# DSH integration example

A runnable example of connecting **DSH** to JarvisMind 1.0. The full walk-through is in
[`docs/tutorials/INTEGRATE-WITH-DSH.md`](../../docs/tutorials/INTEGRATE-WITH-DSH.md).

| Path | What it shows |
| --- | --- |
| `python/setup_agent.py` | Creates the DSH agent and its token through the owner API (the console does the same). |
| `python/dsh_hooks.py` | Pre-Chat context injection and Post-Chat submission, both fail-open. |
| `python/demo_cross_session.py` | End-to-end demo: session A, session B, two-step forget, session C, and the governance boundaries. |
| `typescript/demo.mjs` | The same flow with the TypeScript SDK (ESM JavaScript, typed API, Node 18+). |

## Run it

```bash
# 1. Start a server (see docs/INSTALLATION.md), then create the owner token file once.
export JARVISMIND_URL=http://127.0.0.1:8777

# 2. Create the DSH agent. The token is written to a 0600 file, never printed.
python examples/dsh-integration/python/setup_agent.py \
  --owner-token-file ~/.jarvismind/owner.token \
  --out ~/.jarvismind/dsh-agent.token

# 3. Run the cross-session demo with the agent token.
JARVISMIND_AGENT_TOKEN_FILE=~/.jarvismind/dsh-agent.token \
  python examples/dsh-integration/python/demo_cross_session.py
```

The TypeScript example imports the SDK from `sdk/typescript/dist`, so build it first:

```bash
(cd sdk/typescript && npm install && npm run build)
JARVISMIND_AGENT_TOKEN_FILE=~/.jarvismind/dsh-agent.token \
  node examples/dsh-integration/typescript/demo.mjs
```

The forget step is decided by the owner. Set `JARVISMIND_OWNER_TOKEN` to let the demo
approve as the owner (the same call the console makes); otherwise approve the request in
the console within three minutes.

`JARVISMIND_AUTO_SAVE_ENABLED` is reported by the status endpoint, but it does not gate
explicit `remember` requests: they pass the credential, sensitivity and duplicate checks
and are saved. The demo does not depend on this flag. See
[CONFIGURATION](../../docs/CONFIGURATION.md).
