# @jarvismind/sdk (TypeScript)

A small client for the JarvisMind agent API. It uses the global `fetch` and has no runtime dependencies. Node.js 18 or
newer, or a browser.

```bash
cd sdk/typescript
npm install
npm run build
```

```ts
import { JarvisMindClient, JarvisMindError } from "@jarvismind/sdk";

const client = new JarvisMindClient({ baseUrl: "http://127.0.0.1:8777", token, spaceId: "user-42" });
const receipt = await client.remember({ text: "我喜欢喝乌龙茶", conversationId: "session-123" });
```

- The token is never part of `toString()` or an error message.
- The SDK does not retry. Errors are `JarvisMindError` with `status`, `code`, `details` and `retryAfterSeconds`.
- `remember`, `extract` and `forget` return the server's receipt or request. The SDK has no direct write.

Full method list: [docs/SDK.md](../../docs/SDK.md).

Tests: `npm test` (builds, then runs `node --test`).
