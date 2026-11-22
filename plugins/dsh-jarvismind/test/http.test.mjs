import assert from 'node:assert/strict'
import test from 'node:test'
import { JarvisMindError, JarvisMindHttp } from '../dist/http.js'

// Fake sentinel, not a credential: used to prove the token never leaks.
const TOKEN = 'jmt_test_sentinel_do_not_leak'
const BLOCK = '<jarvismind-context trust="untrusted-data">\n- [preference] tea\n</jarvismind-context>'

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

test('context sends the agent API request with a bearer token', async () => {
  const calls = []
  const http = new JarvisMindHttp('http://127.0.0.1:8777/', () => TOKEN, {
    timeoutMs: 1000,
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init })
      return json({ data: { context: BLOCK, citations: [], retrieval_version: 'v1' } })
    },
  })

  const result = await http.context('年糕', 6)
  assert.equal(result.context, BLOCK)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'http://127.0.0.1:8777/api/v1/agent/context')
  assert.equal(calls[0].init.headers.Authorization, `Bearer ${TOKEN}`)
  assert.deepEqual(JSON.parse(calls[0].init.body), { query: '年糕', limit: 6, max_content_chars: 600 })
})

test('search uses the documented query parameters', async () => {
  const calls = []
  const http = new JarvisMindHttp('http://127.0.0.1:8777', () => TOKEN, {
    timeoutMs: 1000,
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init })
      return json({ data: [{ memory_id: 'mem_1', content: 'tea', memory_type: 'preference' }] })
    },
  })

  const hits = await http.search('tea', 3)
  assert.equal(hits.length, 1)
  assert.equal(calls[0].url, 'http://127.0.0.1:8777/api/v1/agent/search?q=tea&limit=3')
})

test('remember and forget use the governed agent endpoints', async () => {
  const calls = []
  const http = new JarvisMindHttp('http://127.0.0.1:8777', () => TOKEN, {
    timeoutMs: 1000,
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init })
      if (String(url).endsWith('/remember')) return json({ data: { status: 'SAVED', memory_id: 'mem_1' } })
      return json({ data: { id: 'req_1', memory_id: 'mem_1', status: 'PENDING' } })
    },
  })

  const receipt = await http.remember({ text: 'tea', conversationId: 's1', messageId: 'm1', turnId: '2' })
  assert.equal(receipt.status, 'SAVED')
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    text: 'tea',
    conversation_id: 's1',
    message_id: 'm1',
    turn_id: '2',
  })

  const request = await http.forget('mem_1', 'user asked')
  assert.equal(request.status, 'PENDING')
  assert.equal(calls[1].url, 'http://127.0.0.1:8777/api/v1/agent/memories/mem_1/forget-requests')
  assert.deepEqual(JSON.parse(calls[1].init.body), { reason: 'user asked' })
})

test('a missing token fails with not_configured and never echoes a secret', async () => {
  const http = new JarvisMindHttp('http://127.0.0.1:8777', () => undefined, {
    timeoutMs: 1000,
    fetchImpl: async () => {
      throw new Error('must not be called')
    },
  })

  await assert.rejects(http.context('x', 6), (error) => {
    assert.ok(error instanceof JarvisMindError)
    assert.equal(error.code, 'not_configured')
    assert.ok(!error.message.includes(TOKEN))
    return true
  })
})

test('a timeout fails open with code timeout', async () => {
  const http = new JarvisMindHttp('http://127.0.0.1:8777', () => TOKEN, {
    timeoutMs: 25,
    fetchImpl: (_url, init) =>
      new Promise((_resolve, reject) => {
        init.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      }),
  })

  await assert.rejects(http.context('x', 6), (error) => {
    assert.ok(error instanceof JarvisMindError)
    assert.equal(error.code, 'timeout')
    return true
  })
})

test('an HTTP error keeps the server code but redacts token-shaped text', async () => {
  const http = new JarvisMindHttp('http://127.0.0.1:8777', () => TOKEN, {
    timeoutMs: 1000,
    fetchImpl: async () => json({ error: { code: 'upstream_error', message: `bad ${TOKEN}` } }, 502),
  })

  await assert.rejects(http.context('x', 6), (error) => {
    assert.ok(error instanceof JarvisMindError)
    assert.equal(error.code, 'upstream_error')
    assert.equal(error.status, 502)
    assert.ok(!error.message.includes(TOKEN))
    assert.ok(error.message.includes('jmt_[redacted]'))
    return true
  })
})

test('invalid JSON is reported as invalid_response', async () => {
  const http = new JarvisMindHttp('http://127.0.0.1:8777', () => TOKEN, {
    timeoutMs: 1000,
    fetchImpl: async () => new Response('not json', { status: 200 }),
  })
  await assert.rejects(http.context('x', 6), (error) => error.code === 'invalid_response')
})