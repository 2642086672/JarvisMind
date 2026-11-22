import assert from 'node:assert/strict'
import test from 'node:test'
import { RecallCoordinator } from '../dist/recall.js'

const config = {
  baseUrl: 'http://127.0.0.1:8777',
  tokenFile: '~/.jarvismind/dsh-agent.token',
  tokenEnv: 'JARVISMIND_AGENT_TOKEN',
  autoRecall: true,
  recallLimit: 6,
  autoRemember: true,
  timeoutMs: 2500,
}

function userMessage(id, text) {
  return { id, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } }
}

test('one recall per user message is shared by later steps', async () => {
  let calls = 0
  const http = {
    async context() {
      calls += 1
      return { context: '<jarvismind-context>tea</jarvismind-context>' }
    },
  }
  const recall = new RecallCoordinator(http, config, { info() {}, warn() {}, debug() {} })

  recall.noteClaimed('a1', userMessage('m1', 'I like tea'))
  recall.noteClaimed('a1', userMessage('m1', 'I like tea'))

  assert.equal(calls, 1)
  assert.equal(await recall.contextFor('a1'), '<jarvismind-context>tea</jarvismind-context>')
  assert.equal(await recall.contextFor('a1'), '<jarvismind-context>tea</jarvismind-context>')
  assert.equal(calls, 1)
})

test('a new user message starts a new recall', async () => {
  let calls = 0
  const http = {
    async context() {
      calls += 1
      return { context: `<jarvismind-context>${calls}</jarvismind-context>` }
    },
  }
  const recall = new RecallCoordinator(http, config, { info() {}, warn() {}, debug() {} })

  recall.noteClaimed('a1', userMessage('m1', 'first'))
  recall.noteClaimed('a1', userMessage('m2', 'second'))

  assert.equal(calls, 2)
  assert.equal(await recall.contextFor('a1'), '<jarvismind-context>2</jarvismind-context>')
})

test('plugin-injected messages are ignored', () => {
  let calls = 0
  const http = {
    async context() {
      calls += 1
      return { context: '' }
    },
  }
  const recall = new RecallCoordinator(http, config, { info() {}, warn() {}, debug() {} })
  recall.noteClaimed('a1', { id: 'm1', role: 'user', content: [{ type: 'text', text: 'context' }], source: { kind: 'plugin' } })
  assert.equal(calls, 0)
  assert.equal(recall.contextFor('a1'), undefined)
})

test('autoRecall=false performs no request', () => {
  let calls = 0
  const http = {
    async context() {
      calls += 1
      return { context: '' }
    },
  }
  const recall = new RecallCoordinator(http, { ...config, autoRecall: false }, { info() {}, warn() {}, debug() {} })
  recall.noteClaimed('a1', userMessage('m1', 'hello'))
  assert.equal(calls, 0)
})

test('a failed recall resolves to undefined and is logged without content', async () => {
  const logs = []
  const http = {
    async context() {
      throw new Error('network down')
    },
  }
  const recall = new RecallCoordinator(http, config, {
    info() {},
    warn() {},
    debug(...args) {
      logs.push(args.join(' '))
    },
  })

  recall.noteClaimed('a1', userMessage('m1', 'secret user text'))
  assert.equal(await recall.contextFor('a1'), undefined)
  assert.equal(logs.length, 1)
  assert.ok(!logs[0].includes('secret user text'))
})