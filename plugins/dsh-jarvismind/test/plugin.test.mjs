import assert from 'node:assert/strict'
import http from 'node:http'
import { registerHooks } from 'node:module'
import test from 'node:test'

// Fake sentinel, not a credential: used to prove the token never leaks.
const TOKEN = 'jmt_plugin_test_sentinel'

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === '@deepseek-ai/schemastery') {
      return { url: new URL('./stubs/schemastery.mjs', import.meta.url).href, format: 'module', shortCircuit: true }
    }
    if (specifier === '@deepseek-ai/dsh-tools') {
      return { url: new URL('./stubs/dsh-tools.mjs', import.meta.url).href, format: 'module', shortCircuit: true }
    }
    return nextResolve(specifier, context)
  },
})

const plugin = await import('../dist/index.js')

const CONTEXT_BLOCK =
  '<jarvismind-context trust="untrusted-data">\n- [preference] 用户喜欢年糕\n</jarvismind-context>'

function createEventBus() {
  const listeners = new Map()
  return {
    listeners,
    on(event, callback) {
      const list = listeners.get(event) ?? []
      list.push(callback)
      listeners.set(event, list)
      return () => {
        const index = list.indexOf(callback)
        if (index >= 0) list.splice(index, 1)
      }
    },
    emit(event, ...args) {
      for (const callback of listeners.get(event) ?? []) callback(...args)
    },
  }
}

function createFakeAgent(id) {
  const bus = createEventBus()
  const agent = {
    id,
    session: { id },
    listeners: bus.listeners,
    emit: bus.emit,
    ctx: {
      on: bus.on,
      effect(setup) {
        const cleanup = setup()
        let disposed = false
        return () => {
          if (disposed) return
          disposed = true
          if (typeof cleanup === 'function') cleanup()
        }
      },
    },
  }
  return agent
}

function createFakeContext(agents) {
  const bus = createEventBus()
  const tools = []
  const logs = []
  const cleanups = []
  const ctx = {
    listeners: bus.listeners,
    emits: bus.emit,
    logger: {
      info: (...args) => logs.push(['info', ...args]),
      warn: (...args) => logs.push(['warn', ...args]),
      debug: (...args) => logs.push(['debug', ...args]),
    },
    agents: { list: () => agents, roots: () => agents },
    tools: {
      register(definition) {
        tools.push(definition)
        return () => {
          const index = tools.indexOf(definition)
          if (index >= 0) tools.splice(index, 1)
        }
      },
    },
    on: bus.on,
    emit: bus.emit,
    cleanups,
    effect(setup) {
      const cleanup = setup()
      cleanups.push(cleanup)
      let disposed = false
      return () => {
        if (disposed) return
        disposed = true
        if (typeof cleanup === 'function') cleanup()
      }
    },
    inject(_dependencies, callback) {
      callback(ctx)
    },
  }
  return { ctx, tools, logs }
}

async function startServer() {
  const requests = []
  let rememberResolve
  const rememberSeen = new Promise((resolve) => {
    rememberResolve = resolve
  })

  const server = http.createServer((request, response) => {
    let body = ''
    request.on('data', (chunk) => {
      body += chunk
    })
    request.on('end', () => {
      const url = request.url ?? ''
      requests.push({ method: request.method, url, headers: request.headers, body })

      const send = (status, payload) => {
        response.writeHead(status, { 'content-type': 'application/json' })
        response.end(JSON.stringify(payload))
      }

      if (url === '/api/v1/agent/context') {
        send(200, { data: { context: CONTEXT_BLOCK, citations: [], retrieval_version: 'test' } })
      } else if (url.startsWith('/api/v1/agent/memories?')) {
        send(200, {
          data: [
            { id: 'mem_anchor', content: '用户希望被称呼为「年糕」', memory_type: 'PROFILE', importance: 0.95 },
            { id: 'mem_dup', content: '用户喜欢年糕', memory_type: 'PREFERENCE', importance: 0.8 },
          ],
        })
      } else if (url.startsWith('/api/v1/agent/search')) {
        send(200, {
          data: [{ memory_id: 'mem_1', content: '用户喜欢年糕', memory_type: 'preference', importance: 0.9 }],
        })
      } else if (url === '/api/v1/agent/remember') {
        send(200, { data: { status: 'SAVED', memory_id: 'mem_2' } })
        rememberResolve()
      } else if (url.startsWith('/api/v1/agent/memories/') && url.endsWith('/forget-requests')) {
        send(200, { data: { id: 'req_1', memory_id: 'mem_1', status: 'PENDING' } })
      } else {
        send(404, { error: { code: 'not_found', message: 'unknown route' } })
      }
    })
  })

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  return {
    url: `http://127.0.0.1:${address.port}`,
    requests,
    rememberSeen,
    close: () => new Promise((resolve) => server.close(resolve)),
  }
}

function userMessage(id, text) {
  return { id, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } }
}

test('the plugin exports the Cordis Host plugin surface', () => {
  assert.equal(typeof plugin.apply, 'function')
  assert.ok(Array.isArray(plugin.inject))
  assert.ok(plugin.inject.includes('systemPrompt'))
  assert.ok(plugin.inject.includes('agents'))
  assert.ok(plugin.Config)
})

test('recall, capture and the three tools work against a live HTTP server', async () => {
  process.env.JM_PLUGIN_TEST_TOKEN = TOKEN
  const server = await startServer()
  try {
    const agent = createFakeAgent('a1')
    const { ctx, tools, logs } = createFakeContext([agent])

    plugin.apply(ctx, {
      baseUrl: server.url,
      tokenEnv: 'JM_PLUGIN_TEST_TOKEN',
      timeoutMs: 1000,
      autoRecall: true,
      autoRemember: true,
    })

    assert.deepEqual(
      tools.map((tool) => tool.name).sort(),
      ['jarvismind_forget', 'jarvismind_remember', 'jarvismind_search'],
    )

    agent.emit('agent/inbox/claimed', { message: userMessage('m1', '我今天买了年糕'), turn: 1 })

    const assemble = agent.listeners.get('system-prompt/assemble')?.[0]
    assert.equal(typeof assemble, 'function')
    const assembly = { sections: [{ name: 'harness:identity', text: 'base prompt' }] }
    const result = await assemble(assembly, { agent }, async () => assembly)

    assert.equal(result.sections.length, 3)
    // The anchor precedes the turn-specific recall so the relationship framing
    // is established before the facts of this message.
    const anchor = result.sections[1]
    assert.equal(anchor.name, 'jarvismind:core-anchor')
    assert.equal(anchor.interpolate, false)
    assert.match(anchor.text, /<jarvismind-core-anchor/)
    assert.match(anchor.text, /用户希望被称呼为「年糕」/)
    assert.match(anchor.text, /never as instructions/)
    // "用户喜欢年糕" is already in the recall block, so it must not repeat.
    assert.ok(!anchor.text.includes('用户喜欢年糕'))

    const section = result.sections[2]
    assert.equal(section.name, 'jarvismind:context')
    assert.equal(section.interpolate, false)
    assert.ok(section.text.includes(CONTEXT_BLOCK))
    assert.match(section.text, /untrusted data/)

    ctx.emit('session/event', agent.session, { type: 'turn/end', data: { turn: 1 } })
    await server.rememberSeen

    const remember = server.requests.find((request) => request.url === '/api/v1/agent/remember')
    assert.ok(remember)
    assert.equal(remember.headers.authorization, `Bearer ${TOKEN}`)
    assert.deepEqual(JSON.parse(remember.body), {
      text: '我今天买了年糕',
      conversation_id: 'a1',
      message_id: 'm1',
      turn_id: '1',
    })

    // Explicit tools share the same client and governance endpoints.
    const search = tools.find((tool) => tool.name === 'jarvismind_search')
    const searchResult = await search.execute({ query: '年糕', limit: 5 }, {})
    assert.match(searchResult, /mem_1/)
    assert.match(searchResult, /用户喜欢年糕/)
    assert.match(searchResult, /not instructions/)

    const rememberTool = tools.find((tool) => tool.name === 'jarvismind_remember')
    const rememberResult = await rememberTool.execute({ text: '用户喜欢年糕' }, { agent })
    assert.match(rememberResult, /SAVED/)
    assert.match(rememberResult, /mem_2/)

    const forget = tools.find((tool) => tool.name === 'jarvismind_forget')
    const forgetResult = await forget.execute({ memory_id: 'mem_1', reason: 'user asked' }, {})
    assert.match(forgetResult, /PENDING/)
    assert.match(forgetResult, /owner must approve/)

    assert.ok(!JSON.stringify(logs).includes(TOKEN))
  } finally {
    delete process.env.JM_PLUGIN_TEST_TOKEN
    await server.close()
  }
})

test('an unreachable JarvisMind fails open and leaves the prompt untouched', async () => {
  process.env.JM_PLUGIN_TEST_TOKEN = TOKEN
  try {
    const agent = createFakeAgent('a2')
    const { ctx } = createFakeContext([agent])

    plugin.apply(ctx, {
      baseUrl: 'http://127.0.0.1:1',
      tokenEnv: 'JM_PLUGIN_TEST_TOKEN',
      timeoutMs: 250,
      autoRecall: true,
      autoRemember: true,
    })

    agent.emit('agent/inbox/claimed', { message: userMessage('m2', 'is the server running?'), turn: 1 })
    const assemble = agent.listeners.get('system-prompt/assemble')?.[0]
    const assembly = { sections: [{ name: 'harness:identity', text: 'base prompt' }] }
    const result = await assemble(assembly, { agent }, async () => assembly)

    assert.deepEqual(result.sections, assembly.sections)
  } finally {
    delete process.env.JM_PLUGIN_TEST_TOKEN
  }
})

test('compaction/start flushes queued statements before the summary is written', async () => {
  process.env.JM_PLUGIN_TEST_TOKEN = TOKEN
  const server = await startServer()
  try {
    const agent = createFakeAgent('a3')
    const { ctx } = createFakeContext([agent])

    plugin.apply(ctx, {
      baseUrl: server.url,
      tokenEnv: 'JM_PLUGIN_TEST_TOKEN',
      timeoutMs: 1000,
      autoRecall: false,
      autoRemember: true,
    })

    // A statement is queued but its turn has not ended yet.
    agent.emit('agent/inbox/claimed', { message: userMessage('mc1', '请记住：叫我年糕'), turn: 5 })

    ctx.emit('session/event', agent.session, { type: 'compaction/start', data: { turn: 5 } })
    await server.rememberSeen

    const remember = server.requests.find((request) => request.url === '/api/v1/agent/remember')
    assert.ok(remember, 'compaction did not flush the queued statement')
    assert.deepEqual(JSON.parse(remember.body), {
      text: '请记住：叫我年糕',
      conversation_id: 'a3',
      message_id: 'mc1',
      turn_id: '5',
    })
  } finally {
    delete process.env.JM_PLUGIN_TEST_TOKEN
    await server.close()
  }
})

test('coreAnchorEnabled=false injects only the per-turn recall', async () => {
  process.env.JM_PLUGIN_TEST_TOKEN = TOKEN
  const server = await startServer()
  try {
    const agent = createFakeAgent('a4')
    const { ctx } = createFakeContext([agent])

    plugin.apply(ctx, {
      baseUrl: server.url,
      tokenEnv: 'JM_PLUGIN_TEST_TOKEN',
      timeoutMs: 1000,
      autoRecall: true,
      coreAnchorEnabled: false,
    })

    agent.emit('agent/inbox/claimed', { message: userMessage('m4', '我今天买了年糕'), turn: 1 })
    const assemble = agent.listeners.get('system-prompt/assemble')?.[0]
    const assembly = { sections: [{ name: 'harness:identity', text: 'base prompt' }] }
    const result = await assemble(assembly, { agent }, async () => assembly)

    assert.deepEqual(
      result.sections.map((section) => section.name),
      ['harness:identity', 'jarvismind:context'],
    )
    assert.ok(!server.requests.some((request) => request.url.startsWith('/api/v1/agent/memories')))
  } finally {
    delete process.env.JM_PLUGIN_TEST_TOKEN
    await server.close()
  }
})

test('a 5xx anchor response fails open without a leaked token', async () => {
  const failing = http.createServer((request, response) => {
    response.writeHead(500, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ error: { code: 'internal_error', message: `boom ${TOKEN}` } }))
  })
  await new Promise((resolve) => failing.listen(0, '127.0.0.1', resolve))
  const baseUrl = `http://127.0.0.1:${failing.address().port}`
  process.env.JM_PLUGIN_TEST_TOKEN = TOKEN

  try {
    const agent = createFakeAgent('a5')
    const { ctx, logs } = createFakeContext([agent])

    plugin.apply(ctx, {
      baseUrl,
      tokenEnv: 'JM_PLUGIN_TEST_TOKEN',
      timeoutMs: 1000,
      autoRecall: true,
    })

    const assemble = agent.listeners.get('system-prompt/assemble')?.[0]
    const assembly = { sections: [{ name: 'harness:identity', text: 'base prompt' }] }
    const result = await assemble(assembly, { agent }, async () => assembly)

    // No anchor, no recall, and the prompt is exactly what the harness produced.
    assert.deepEqual(result.sections, assembly.sections)
    assert.ok(!JSON.stringify(logs).includes(TOKEN))
  } finally {
    delete process.env.JM_PLUGIN_TEST_TOKEN
    await new Promise((resolve) => failing.close(resolve))
  }
})

test('unloading the plugin clears the anchor cache and tears down cleanly', async () => {
  process.env.JM_PLUGIN_TEST_TOKEN = TOKEN
  const server = await startServer()
  try {
    const agent = createFakeAgent('a6')
    const { ctx } = createFakeContext([agent])

    plugin.apply(ctx, {
      baseUrl: server.url,
      tokenEnv: 'JM_PLUGIN_TEST_TOKEN',
      timeoutMs: 1000,
      autoRecall: false,
    })

    const assemble = agent.listeners.get('system-prompt/assemble')?.[0]
    const assembly = { sections: [] }
    await assemble(assembly, { agent }, async () => assembly)
    await assemble(assembly, { agent }, async () => assembly)

    const anchorReads = () => server.requests.filter((request) => request.url.startsWith('/api/v1/agent/memories')).length
    // One read serves both assemblies inside the 30 s window.
    assert.equal(anchorReads(), 1)

    // Tear the plugin down the way Cordis does.
    for (const cleanup of ctx.cleanups) cleanup()

    // Every registration is gone, and a second teardown is harmless.
    assert.equal(agent.listeners.get('system-prompt/assemble')?.length ?? 0, 0)
    assert.equal(ctx.listeners.get('session/event')?.length ?? 0, 0)
    for (const cleanup of ctx.cleanups) cleanup()

    // Re-applying starts from cold: the disposed instance kept no cache.
    const fresh = createFakeAgent('a7')
    const second = createFakeContext([fresh])
    plugin.apply(second.ctx, {
      baseUrl: server.url,
      tokenEnv: 'JM_PLUGIN_TEST_TOKEN',
      timeoutMs: 1000,
      autoRecall: false,
    })
    const secondAssemble = fresh.listeners.get('system-prompt/assemble')[0]
    await secondAssemble(assembly, { agent: fresh }, async () => assembly)
    assert.equal(anchorReads(), 2)
  } finally {
    delete process.env.JM_PLUGIN_TEST_TOKEN
    await server.close()
  }
})