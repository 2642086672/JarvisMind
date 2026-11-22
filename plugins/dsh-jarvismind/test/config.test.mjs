import assert from 'node:assert/strict'
import test from 'node:test'
import { DEFAULT_BASE_URL, DEFAULT_CORE_ANCHOR_LIMIT, DEFAULT_CORE_ANCHOR_MAX_CHARS, DEFAULT_RECALL_LIMIT, DEFAULT_TIMEOUT_MS, normalizeConfig } from '../dist/config.js'

test('empty input yields the documented defaults', () => {
  const config = normalizeConfig(undefined)
  assert.equal(config.baseUrl, DEFAULT_BASE_URL)
  assert.equal(config.tokenFile, '~/.jarvismind/dsh-agent.token')
  assert.equal(config.tokenEnv, 'JARVISMIND_AGENT_TOKEN')
  assert.equal(config.autoRecall, true)
  assert.equal(config.recallLimit, DEFAULT_RECALL_LIMIT)
  assert.equal(config.autoRemember, true)
  assert.equal(config.coreAnchorEnabled, true)
  assert.equal(config.coreAnchorLimit, DEFAULT_CORE_ANCHOR_LIMIT)
  assert.equal(config.coreAnchorMaxChars, DEFAULT_CORE_ANCHOR_MAX_CHARS)
  assert.equal(config.timeoutMs, DEFAULT_TIMEOUT_MS)
})

test('baseUrl keeps a base path but drops credentials, query and trailing slashes', () => {
  assert.equal(normalizeConfig({ baseUrl: 'http://user:pass@127.0.0.1:8777/' }).baseUrl, 'http://127.0.0.1:8777')
  assert.equal(normalizeConfig({ baseUrl: 'https://example.test/jm//' }).baseUrl, 'https://example.test/jm')
  assert.equal(normalizeConfig({ baseUrl: 'http://127.0.0.1:8777/?token=no' }).baseUrl, 'http://127.0.0.1:8777')
  assert.equal(normalizeConfig({ baseUrl: 'ftp://example.test' }).baseUrl, DEFAULT_BASE_URL)
  assert.equal(normalizeConfig({ baseUrl: 'not a url' }).baseUrl, DEFAULT_BASE_URL)
})

test('numeric bounds are clamped and booleans are strict', () => {
  assert.equal(normalizeConfig({ recallLimit: 0 }).recallLimit, 1)
  assert.equal(normalizeConfig({ recallLimit: 999 }).recallLimit, 20)
  assert.equal(normalizeConfig({ recallLimit: 3.9 }).recallLimit, 3)
  assert.equal(normalizeConfig({ timeoutMs: 1 }).timeoutMs, 200)
  assert.equal(normalizeConfig({ timeoutMs: 999_999 }).timeoutMs, 30_000)
  assert.equal(normalizeConfig({ autoRecall: false }).autoRecall, false)
  assert.equal(normalizeConfig({ autoRemember: false }).autoRemember, false)
})

test('core anchor bounds are clamped to the documented ranges', () => {
  assert.equal(normalizeConfig({ coreAnchorLimit: 0 }).coreAnchorLimit, 1)
  assert.equal(normalizeConfig({ coreAnchorLimit: 99 }).coreAnchorLimit, 12)
  assert.equal(normalizeConfig({ coreAnchorLimit: 4.8 }).coreAnchorLimit, 4)
  assert.equal(normalizeConfig({ coreAnchorMaxChars: 1 }).coreAnchorMaxChars, 100)
  assert.equal(normalizeConfig({ coreAnchorMaxChars: 999_999 }).coreAnchorMaxChars, 2000)
  assert.equal(normalizeConfig({ coreAnchorEnabled: false }).coreAnchorEnabled, false)
  // A typo must not disable the anchor; it falls back to the default.
  assert.equal(normalizeConfig({ coreAnchorLimit: 'many' }).coreAnchorLimit, DEFAULT_CORE_ANCHOR_LIMIT)
})