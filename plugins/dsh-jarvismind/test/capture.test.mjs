import assert from 'node:assert/strict'
import test from 'node:test'
import { CaptureQueue, isSubstantive } from '../dist/capture.js'

const config = {
  baseUrl: 'http://127.0.0.1:8777',
  tokenFile: '~/.jarvismind/dsh-agent.token',
  tokenEnv: 'JARVISMIND_AGENT_TOKEN',
  autoRecall: true,
  recallLimit: 6,
  autoRemember: true,
  // These cases describe the original turn/end capture, which the `auto` and
  // `both` modes keep. The default `agent` mode is covered separately below.
  rememberMode: 'auto',
  fallbackCaptureOnTurnEnd: true,
  memoryStyle: 'assistant',
  coreAnchorEnabled: true,
  coreAnchorLimit: 6,
  coreAnchorMaxChars: 500,
  timeoutMs: 2500,
}

function userMessage(id, text) {
  return { id, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } }
}

test('turnEnded returns immediately and submits the user statement', async () => {
  const calls = []
  let release
  const http = {
    remember(input) {
      calls.push(input)
      return new Promise((resolve) => {
        release = resolve
      })
    },
  }
  const capture = new CaptureQueue(http, config, { info() {}, warn() {}, debug() {} })

  capture.noteClaimed('a1', userMessage('m1', 'I bought 年糕'), 3)
  assert.equal(capture.pendingCount(), 1)

  const returned = capture.turnEnded('a1', 's1', 3)
  assert.equal(returned, undefined)
  assert.equal(capture.pendingCount(), 0)
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0], {
    text: 'I bought 年糕',
    conversationId: 's1',
    messageId: 'm1',
    turnId: '3',
    // An automatic capture is a raw user statement, so the server's own intent
    // and credential gates must judge it. Sent explicitly, not by default.
    mode: 'auto',
  })

  const drained = capture.drain()
  release({ status: 'SAVED' })
  await drained
})

test('a failed submission is swallowed and logged without content', async () => {
  const logs = []
  const http = {
    async remember() {
      throw new Error('server down')
    },
  }
  const capture = new CaptureQueue(http, config, {
    info() {},
    warn() {},
    debug(...args) {
      logs.push(args.join(' '))
    },
  })

  capture.noteClaimed('a1', userMessage('m1', 'private text'), 1)
  capture.turnEnded('a1', 's1', 1)
  await capture.drain()

  assert.equal(logs.length, 1)
  assert.ok(!logs[0].includes('private text'))
})

test('only user-authored messages are captured', () => {
  let calls = 0
  const http = {
    async remember() {
      calls += 1
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(http, config, { info() {}, warn() {}, debug() {} })

  capture.noteClaimed('a1', { id: 'm1', role: 'user', content: [{ type: 'text', text: 'x' }], source: { kind: 'plugin' } }, 1)
  capture.noteClaimed('a2', { id: 'm2', role: 'assistant', content: [{ type: 'text', text: 'y' }], source: { kind: 'model' } }, 1)
  capture.turnEnded('a1', 's1', 1)
  capture.turnEnded('a2', 's2', 1)
  assert.equal(calls, 0)
  assert.equal(capture.pendingCount(), 0)
})

test('autoRemember=false captures nothing', () => {
  let calls = 0
  const http = {
    async remember() {
      calls += 1
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(http, { ...config, autoRemember: false }, { info() {}, warn() {}, debug() {} })
  capture.noteClaimed('a1', userMessage('m1', 'x'), 1)
  capture.turnEnded('a1', 's1', 1)
  assert.equal(calls, 0)
})

test('compactionStarted flushes every queued turn immediately', async () => {
  const calls = []
  const http = {
    async remember(input) {
      calls.push(input)
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(http, config, { info() {}, warn() {}, debug() {} })

  capture.noteClaimed('a1', userMessage('m1', 'first'), 1)
  capture.noteClaimed('a1', userMessage('m2', 'second'), 2)
  assert.equal(capture.pendingCount(), 2)

  // `compaction/start` is not tied to a turn, so every queued statement goes.
  const returned = capture.compactionStarted('a1', 's1')
  assert.equal(returned, undefined)
  assert.equal(capture.pendingCount(), 0)
  await capture.drain()

  assert.deepEqual(
    calls.map((call) => call.text),
    ['first', 'second'],
  )
  assert.deepEqual(
    calls.map((call) => call.turnId),
    ['1', '2'],
  )
})

test('compactionStarted respects autoRemember=false', () => {
  let calls = 0
  const http = {
    async remember() {
      calls += 1
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(http, { ...config, autoRemember: false }, { info() {}, warn() {}, debug() {} })
  capture.noteClaimed('a1', userMessage('m1', 'x'), 1)
  capture.compactionStarted('a1', 's1')
  assert.equal(calls, 0)
  // With capture disabled nothing is ever queued in the first place.
  assert.equal(capture.pendingCount(), 0)
})

test('compactionStarted on an idle agent is a no-op', () => {
  const http = {
    async remember() {
      throw new Error('must not be called')
    },
  }
  const capture = new CaptureQueue(http, config, { info() {}, warn() {}, debug() {} })
  assert.equal(capture.compactionStarted('missing', 's1'), undefined)
})

// ---------------------------------------------------------------------------
// rememberMode
// ---------------------------------------------------------------------------

test('rememberMode=agent with the fallback still refuses a pleasantry or a one-word reply', async () => {
  const calls = []
  const http = {
    async remember(input) {
      calls.push(input)
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(
    http,
    { ...config, rememberMode: 'agent' },
    { info() {}, warn() {}, debug() {} },
  )

  // The noise the fallback exists to avoid: acknowledgements, punctuation and a
  // single character state nothing, so they are never even offered to the server.
  capture.noteClaimed('a1', userMessage('m1', '嗯'), 1)
  capture.noteClaimed('a1', userMessage('m2', '谢谢'), 2)
  capture.noteClaimed('a1', userMessage('m3', '好的！'), 3)
  capture.noteClaimed('a1', userMessage('m4', '。。。'), 4)
  capture.noteClaimed('a1', userMessage('m5', 'ok'), 5)
  assert.equal(capture.pendingCount(), 0)

  capture.turnEnded('a1', 's1', 1)
  capture.turnEnded('a1', 's1', 2)
  capture.turnEnded('a1', 's1', 3)
  capture.turnEnded('a1', 's1', 4)
  capture.turnEnded('a1', 's1', 5)
  capture.compactionStarted('a1', 's1')
  await capture.drain()

  assert.equal(calls.length, 0)
})

test('rememberMode=agent submits a substantive turn when the model did not remember', async () => {
  const calls = []
  const http = {
    async remember(input) {
      calls.push(input)
      return { status: 'NO_INTENT' }
    },
  }
  const capture = new CaptureQueue(
    http,
    { ...config, rememberMode: 'agent' },
    { info() {}, warn() {}, debug() {} },
  )

  capture.noteClaimed('a1', userMessage('m1', '用户平时在 Mac 上开发 JarvisMind。'), 1)
  assert.equal(capture.pendingCount(), 1)

  capture.turnEnded('a1', 's1', 1)
  await capture.drain()

  assert.equal(calls.length, 1)
  // `auto`, not `direct`: the fallback is a raw user turn, so the server's own
  // intent and credential gates must still judge it. It can legitimately answer
  // NO_INTENT and store nothing.
  assert.equal(calls[0].mode, 'auto')
  assert.equal(calls[0].text, '用户平时在 Mac 上开发 JarvisMind。')
})

test('rememberMode=agent never duplicates a turn the model already remembered', async () => {
  const calls = []
  const http = {
    async remember(input) {
      calls.push(input)
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(
    http,
    { ...config, rememberMode: 'agent' },
    { info() {}, warn() {}, debug() {} },
  )

  capture.noteClaimed('a1', userMessage('m1', '用户希望被称呼为「年糕」'), 4)
  // The model called jarvismind_remember during this turn.
  capture.markRemembered('a1')
  assert.equal(capture.pendingCount(), 1)

  capture.turnEnded('a1', 's1', 4)
  await capture.drain()

  assert.equal(calls.length, 0, 'the fallback submitted a turn the model already remembered')
  // The queued copy is discarded, not left to be flushed at compaction.
  assert.equal(capture.pendingCount(), 0)
})

test('a remembered turn does not silence the next turn', async () => {
  const calls = []
  const http = {
    async remember(input) {
      calls.push(input)
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(
    http,
    { ...config, rememberMode: 'agent' },
    { info() {}, warn() {}, debug() {} },
  )

  capture.noteClaimed('a1', userMessage('m1', 'turn four'), 4)
  capture.markRemembered('a1')
  capture.turnEnded('a1', 's1', 4)
  await capture.drain()
  assert.equal(calls.length, 0)

  // A later turn where the model said nothing must still use the fallback.
  capture.noteClaimed('a1', userMessage('m2', '我下周去杭州出差'), 5)
  capture.turnEnded('a1', 's1', 5)
  await capture.drain()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].text, '我下周去杭州出差')
})

test('fallbackCaptureOnTurnEnd=false restores pure model-decided capture', async () => {
  const calls = []
  const http = {
    async remember(input) {
      calls.push(input)
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(
    http,
    { ...config, rememberMode: 'agent', fallbackCaptureOnTurnEnd: false },
    { info() {}, warn() {}, debug() {} },
  )

  capture.noteClaimed('a1', userMessage('m1', '用户平时在 Mac 上开发 JarvisMind。'), 1)
  assert.equal(capture.pendingCount(), 0)
  capture.turnEnded('a1', 's1', 1)
  capture.compactionStarted('a1', 's1')
  await capture.drain()

  assert.equal(calls.length, 0)
})

test('compaction/start drops a turn the model already remembered', async () => {
  const calls = []
  const http = {
    async remember(input) {
      calls.push(input)
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(
    http,
    { ...config, rememberMode: 'agent' },
    { info() {}, warn() {}, debug() {} },
  )

  capture.noteClaimed('a1', userMessage('m1', '用户希望被称呼为「年糕」'), 2)
  capture.markRemembered('a1')

  capture.compactionStarted('a1', 's1')
  await capture.drain()

  assert.equal(calls.length, 0, 'compaction flushed a turn the model already remembered')
  assert.equal(capture.pendingCount(), 0)
})

test('clear() forgets a disposed agent entirely', () => {
  const http = { async remember() { return { status: 'SAVED' } } }
  const capture = new CaptureQueue(http, { ...config, rememberMode: 'agent' }, { info() {}, warn() {}, debug() {} })
  capture.noteClaimed('a1', userMessage('m1', '用户平时在 Mac 上开发 JarvisMind。'), 1)
  assert.equal(capture.pendingCount(), 1)
  capture.clear('a1')
  assert.equal(capture.pendingCount(), 0)
})

test('rememberMode=both captures every turn and still leaves the tool available', async () => {
  const calls = []
  const http = {
    async remember(input) {
      calls.push(input)
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(
    http,
    { ...config, rememberMode: 'both' },
    { info() {}, warn() {}, debug() {} },
  )

  capture.noteClaimed('a1', userMessage('m1', '我在写 Rust'), 1)
  assert.equal(capture.pendingCount(), 1)
  capture.turnEnded('a1', 's1', 1)
  await capture.drain()
  assert.deepEqual(
    calls.map((call) => call.text),
    ['我在写 Rust'],
  )
})

test('rememberMode=agent keeps autoRemember=false meaning no capture at all', () => {
  let calls = 0
  const http = {
    async remember() {
      calls += 1
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(
    http,
    { ...config, rememberMode: 'agent', autoRemember: false },
    { info() {}, warn() {}, debug() {} },
  )
  capture.noteClaimed('a1', userMessage('m1', 'x'), 1)
  capture.turnEnded('a1', 's1', 1)
  assert.equal(calls, 0)
  assert.equal(capture.pendingCount(), 0)
})// ---------------------------------------------------------------------------
// isSubstantive
// ---------------------------------------------------------------------------

test('isSubstantive keeps real statements and rejects only noise', () => {
  // Real statements, including short ones.
  for (const text of ['我叫年糕', '用户平时在 Mac 上开发 JarvisMind。', '我在写 Rust', '下周去杭州', '嗯，我叫年糕']) {
    assert.equal(isSubstantive(text), true, `expected substantive: ${text}`)
  }

  // Acknowledgements, punctuation and whitespace carry no fact.
  for (const text of ['', '   ', '嗯', '好', '好的', '好的！', '谢谢', '谢谢！', '收到', 'ok', 'OK', 'thanks', '。。。', '…', '👍', '👍👍']) {
    assert.equal(isSubstantive(text), false, `expected noise: ${JSON.stringify(text)}`)
  }
})