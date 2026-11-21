import { test } from "node:test";
import assert from "node:assert/strict";
import { JarvisMindClient, JarvisMindError } from "../dist/index.js";

function recorder(respond) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), init });
    return respond(String(url), init);
  };
  return { fetchImpl, calls };
}

function json(status, payload, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

test("sends the bearer token, the space header and a normalised URL", async () => {
  const { fetchImpl, calls } = recorder(() =>
    json(200, { data: { items: [], total: 0, retrieval_version: "context-v1", degraded_to_fts: false } }),
  );
  const client = new JarvisMindClient({
    baseUrl: "http://127.0.0.1:8777/",
    token: "jmt_example_secret",
    spaceId: "work-space",
    fetchImpl,
  });
  const result = await client.recall("绿茶", { limit: 3 });
  assert.equal(result.total, 0);
  assert.equal(calls[0].url, "http://127.0.0.1:8777/api/v1/agent/recall");
  assert.equal(calls[0].init.headers.Authorization, "Bearer jmt_example_secret");
  assert.equal(calls[0].init.headers["X-JarvisMind-Space"], "work-space");
  assert.deepEqual(JSON.parse(calls[0].init.body), { query: "绿茶", limit: 3 });
});

test("never shows the token in its string form", () => {
  const client = new JarvisMindClient({ baseUrl: "http://127.0.0.1:8777", token: "jmt_example_secret", fetchImpl: async () => json(200, {}) });
  assert.equal(String(client).includes("jmt_example_secret"), false);
});

test("remember omits unset optional fields", async () => {
  const { fetchImpl, calls } = recorder(() => json(200, { data: { status: "SAVED" } }));
  const client = new JarvisMindClient({ baseUrl: "http://127.0.0.1:8777", token: "jmt_example_secret", fetchImpl });
  const result = await client.remember({ text: "我喜欢喝绿茶", conversationId: "session-a" });
  assert.equal(result.status, "SAVED");
  assert.deepEqual(JSON.parse(calls[0].init.body), { text: "我喜欢喝绿茶", conversation_id: "session-a" });
});

test("forget never exposes a confirmation token", async () => {
  const { fetchImpl, calls } = recorder(() =>
    json(201, { data: { id: "fr_1", memory_id: "mem_1", status: "PENDING", requester_label: "agent:x", requested_at: "t", reused_existing: false } }),
  );
  const client = new JarvisMindClient({ baseUrl: "http://127.0.0.1:8777", token: "jmt_example_secret", fetchImpl });
  const created = await client.forget("mem_1", "用户要求删除");
  assert.equal(created.status, "PENDING");
  assert.equal("confirmation_token" in created, false);
  assert.equal(calls[0].url, "http://127.0.0.1:8777/api/v1/agent/memories/mem_1/forget-requests");
});

test("update is refused for agents with a typed error", async () => {
  const { fetchImpl } = recorder(() =>
    json(403, { error: { code: "non_human_actor_forbidden", message: "agents cannot request corrections" } }),
  );
  const client = new JarvisMindClient({ baseUrl: "http://127.0.0.1:8777", token: "jmt_example_secret", fetchImpl });
  await assert.rejects(
    () => client.update("mem_1", { content: "x" }),
    (error) => error instanceof JarvisMindError && error.status === 403 && error.code === "non_human_actor_forbidden",
  );
});

test("rate limits expose Retry-After", async () => {
  const { fetchImpl } = recorder(() => json(429, { error: { code: "rate_limited", message: "slow down" } }, { "Retry-After": "7" }));
  const client = new JarvisMindClient({ baseUrl: "http://127.0.0.1:8777", token: "jmt_example_secret", fetchImpl });
  await assert.rejects(
    () => client.me(),
    (error) => error instanceof JarvisMindError && error.retryAfterSeconds === 7,
  );
});

test("an unreachable server raises a network error", async () => {
  const fetchImpl = async () => {
    throw new TypeError("fetch failed");
  };
  const client = new JarvisMindClient({ baseUrl: "http://127.0.0.1:8777", token: "jmt_example_secret", fetchImpl });
  await assert.rejects(
    () => client.me(),
    (error) => error instanceof JarvisMindError && error.code === "network_error",
  );
});

test("memory identifiers are URL-encoded", async () => {
  const { fetchImpl, calls } = recorder(() => json(200, { data: { id: "a/b" } }));
  const client = new JarvisMindClient({ baseUrl: "http://127.0.0.1:8777", token: "jmt_example_secret", fetchImpl });
  await client.getMemory("a/b");
  assert.equal(calls[0].url, "http://127.0.0.1:8777/api/v1/agent/memories/a%2Fb");
});

test("base URL must be http or https", () => {
  assert.throws(() => new JarvisMindClient({ baseUrl: "ftp://example.invalid", token: "jmt_example_secret" }));
});
