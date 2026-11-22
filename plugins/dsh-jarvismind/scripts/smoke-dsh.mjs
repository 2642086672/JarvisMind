#!/usr/bin/env node
/**
 * Smoke-test the built plugin against the real DSH Desktop packages.
 *
 * Plain `node` cannot read `app.asar`; run this with the DSH Electron binary:
 *
 *   npm run build
 *   ELECTRON_RUN_AS_NODE=1 \
 *     "/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness" \
 *     --expose-internals scripts/smoke-dsh.mjs
 *
 * The script resolves @deepseek-ai packages from the installed app.asar, loads
 * dist/index.js, runs the plugin with a fake Cordis surface, and drives
 * recall/capture plus the three tools against a local HTTP stub. It prints
 * "SMOKE OK" and exits 0 on success.
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import http from 'node:http'
import { registerHooks } from 'node:module'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const appRoot = process.env.DSH_APP_PATH ?? '/Applications/DeepSeek Harness.app'
const nodeModules = path.join(appRoot, 'Contents/Resources/app.asar/dsh/node_modules')
const pluginEntry = pathToFileURL(path.join(import.meta.dirname, '..', 'dist', 'index.js')).href

if (process.versions.electron === undefined) {
  console.error('Run this script with the DSH Electron binary (ELECTRON_RUN_AS_NODE=1); plain node cannot read app.asar.')
  process.exit(2)
}
if (!fs.existsSync(nodeModules)) {
  console.error(`DSH packages not found at ${nodeModules}. Set DSH_APP_PATH to the installed app.`)
  process.exit(2)
}

function pickExport(value) {
  if (typeof value === 'string') return value
  return value?.import ?? value?.node ?? value?.default ?? value?.require
}

function entryUrl(specifier) {
  const parts = specifier.split('/')
  const name = specifier.startsWith('@') ? parts.slice(0, 2).join('/') : parts[0]
  const sub = specifier.startsWith('@') ? parts.slice(2).join('/') : parts.slice(1).join('/')
  const dir = path.join(nodeModules, name)
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'))
  const key = sub ? `./${sub}` : '.'
  let relative = pickExport(manifest.exports?.[key])
  if (relative === undefined) {
    for (const [pattern, value] of Object.entries(manifest.exports ?? {})) {
      if (!pattern.includes('*')) continue
      const [prefix, suffix] = pattern.split('*')
      if (!key.startsWith(prefix) || !key.endsWith(suffix)) continue
      const star = key.slice(prefix.length, key.length - suffix.length)
      const target = pickExport(value)
      if (target !== undefined) {
        relative = target.replace('*', star)
        break
      }
    }
  }
  relative ??= sub || manifest.module || manifest.main || 'index.js'
  return pathToFileURL(path.join(dir, relative)).href
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('@deepseek-ai/') || specifier.startsWith('@standard-schema/')) {
      return { url: entryUrl(specifier), shortCircuit: true }
    }
    return nextResolve(specifier, context)
  },
})

const plugin = await import(pluginEntry)

function createBus() {
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

function createAgent(id) {
  const bus = createBus()
  return {
    id,
    session: { id },
    listeners: bus.listeners,
    emit: bus.emit,
    ctx: {
      on: bus.on,
      effect(setup) {
        const cleanup = setup()
        return () => cleanup?.()
      },
    },
  }
}

function createContext(agents) {
  const bus = createBus()
  const tools = []
  const logs = []
  const ctx = {
    logger: {
      info: (...args) => logs.push(['info', ...args]),
      warn: (...args) => logs.push(['warn', ...args]),
      debug: (...args) => logs.push(['debug', ...args]),
    },
    agents: { list: () => agents, roots: () => agents },
    tools: {
      register(definition) {
        tools.push(definition)
        return () => {}
      },
    },
    on: bus.on,
    emit: bus.emit,
    effect(setup) {
      const cleanup = setup()
      return () => cleanup?.()
    },
    inject(_dependencies, callback) {
      callback(ctx)
    },
  }
  return { ctx, tools, logs }
}

async function startStub() {
  const requests = []
  let resolveRemember
  const rememberSeen = new Promise((resolve) => {
    resolveRemember = resolve
  })
  const server = http.createServer((request, response) => {
    let body = ''
    request.on('data', (chunk) => {
      body += chunk
    })
    request.on('end', () => {
      const url = request.url ?? ''
      requests.push({ url, headers: request.headers, body })
      response.writeHead(200, { 'content-type': 'application/json' })
      if (url === '/api/v1/agent/context') {
        response.end(
          JSON.stringify({
            data: {
              context: '<jarvismind-context trust="untrusted-data">\n- [preference] tea\n</jarvismind-context>',
              citations: [],
              retrieval_version: 'smoke',
            },
          }),
        )
      } else if (url.startsWith('/api/v1/agent/memories?')) {
        response.end(
          JSON.stringify({
            data: [
              { id: 'mem_anchor', content: '用户希望被称呼为「年糕」', memory_type: 'PROFILE', importance: 0.95 },
              { id: 'mem_dup', content: 'tea', memory_type: 'PREFERENCE', importance: 0.8 },
            ],
          }),
        )
      } else if (url === '/api/v1/agent/remember') {
        response.end(JSON.stringify({ data: { status: 'SAVED', memory_id: 'mem_1' } }))
        resolveRemember()
      } else {
        response.end(JSON.stringify({ data: [] }))
      }
    })
  })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    requests,
    rememberSeen,
    close: () => new Promise((resolve) => server.close(resolve)),
  }
}

// Fake sentinel, not a credential: used to prove the token never leaks.
const TOKEN = 'jmt_smoke_sentinel'
process.env.JM_DSH_SMOKE_TOKEN = TOKEN
const stub = await startStub()
const agent = createAgent('smoke-agent')
const { ctx, tools, logs } = createContext([agent])

plugin.apply(ctx, { baseUrl: stub.url, tokenEnv: 'JM_DSH_SMOKE_TOKEN', timeoutMs: 1000 })

assert.deepEqual(
  tools.map((tool) => tool.name).sort(),
  ['jarvismind_forget', 'jarvismind_remember', 'jarvismind_search'],
)
for (const tool of tools) {
  assert.equal(tool.parameters?.type, 'object', `${tool.name} parameters are not a JSON Schema object`)
  assert.equal(tool.output?.schema?.type, 'string', `${tool.name} output schema is not a string`)
}

agent.emit('agent/inbox/claimed', {
  message: { id: 'm1', role: 'user', content: [{ type: 'text', text: 'I like tea' }], source: { kind: 'user' } },
  turn: 1,
})
const assembly = { sections: [{ name: 'harness:identity', text: 'base' }] }
const assembled = await agent.listeners.get('system-prompt/assemble')[0](assembly, { agent }, async () => assembly)

// Under the default `rememberMode: agent` the order is: the harness's own
// section, then the selection brief, then the always-on anchor, then the
// per-turn recall. The brief comes before the anchor because the model reads
// "what is worth recording" before it reads the relationship data.
assert.equal(assembled.sections.length, 4)
assert.equal(assembled.sections[1].name, 'jarvismind:remember-guide')
assert.equal(assembled.sections[1].interpolate, false)

const anchor = assembled.sections[2]
assert.equal(anchor.name, 'jarvismind:core-anchor')
assert.equal(anchor.interpolate, false)
assert.match(anchor.text, /<jarvismind-core-anchor/)
assert.match(anchor.text, /用户希望被称呼为「年糕」/)
assert.match(anchor.text, /never as instructions/)
// "tea" is already in the per-turn recall block and must not repeat here.
assert.ok(!anchor.text.includes('tea'), 'anchor repeated a recalled memory')

assert.equal(assembled.sections[3].name, 'jarvismind:context')
assert.match(assembled.sections[3].text, /untrusted data/)

// Real DSH accepted the Config schema, including the three new anchor keys.
assert.ok(plugin.Config, 'Config was not exported')
const resolved = plugin.Config({})
assert.equal(resolved.coreAnchorEnabled, true, 'coreAnchorEnabled default')
assert.equal(resolved.coreAnchorLimit, 6, 'coreAnchorLimit default')
assert.equal(resolved.coreAnchorMaxChars, 500, 'coreAnchorMaxChars default')

// In the default `agent` mode a turn ending must NOT submit anything by itself:
// submitting every turn is what used to file "嗯" and "谢谢" as memory
// candidates. The model calls the tool instead.
ctx.emit('session/event', agent.session, { type: 'turn/end', data: { turn: 1 } })
await new Promise((resolve) => setTimeout(resolve, 100))
assert.ok(
  !stub.requests.some((request) => request.url === '/api/v1/agent/remember'),
  'a turn was captured automatically in agent mode',
)

// The model's own decision goes through the tool, and it must be sent as
// `mode: "direct"`: the model has judged it worth keeping, so the server should
// not re-apply its trigger-word heuristic. Governance still runs server-side.
const rememberTool = tools.find((tool) => tool.name === 'jarvismind_remember')
assert.ok(rememberTool, 'jarvismind_remember was not registered')
const toolResult = await rememberTool.execute(
  { text: '用户希望被称呼为「年糕」' },
  { agent, signal: undefined },
)
assert.equal(typeof toolResult, 'string', 'tool did not return a rendered string')
await stub.rememberSeen
const remember = stub.requests.find((request) => request.url === '/api/v1/agent/remember')
assert.ok(remember, 'remember request was not sent')
const rememberBody = JSON.parse(remember.body)
assert.equal(rememberBody.text, '用户希望被称呼为「年糕」')
assert.equal(rememberBody.mode, 'direct', 'the tool must send mode=direct in agent mode')
assert.ok(!JSON.stringify(logs).includes(TOKEN), 'token leaked into logs')

await stub.close()
console.log('SMOKE OK', JSON.stringify({ tools: tools.map((tool) => tool.name), requests: stub.requests.map((r) => r.url) }))