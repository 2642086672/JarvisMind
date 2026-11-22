import assert from 'node:assert/strict'
import test from 'node:test'
import { CaptureQueue } from '../dist/capture.js'

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

test('rememberMode=agent never captures a turn mechanically', async () => {
  let calls = 0
  const http = {
    async remember() {
      calls += 1
      return { status: 'SAVED' }
    },
  }
  const capture = new CaptureQueue(
    http,
    { ...config, rememberMode: 'agent' },
    { info() {}, warn() {}, debug() {} },
  )

  // The noise this mode exists to filter: acknowledgements and a transient log.
  capture.noteClaimed('a1', userMessage('m1', '嗯'), 1)
  capture.noteClaimed('a1', userMessage('m2', '谢谢'), 2)
  capture.noteClaimed('a1', userMessage('m3', 'TypeError: undefined is not a function'), 3)
  assert.equal(capture.pendingCount(), 0)

  capture.turnEnded('a1', 's1', 1)
  capture.turnEnded('a1', 's1', 2)
  capture.turnEnded('a1', 's1', 3)
  capture.compactionStarted('a1', 's1')
  await capture.drain()

  assert.equal(calls, 0)
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
})