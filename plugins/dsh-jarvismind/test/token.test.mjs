import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveToken, expandHome } from '../dist/token.js'

const CONFIG = { tokenEnv: 'JM_TEST_TOKEN', tokenFile: '~/.jarvismind/dsh-agent.token' }

test('the environment variable wins over the token file', () => {
  const token = resolveToken(CONFIG, { JM_TEST_TOKEN: '  jmt_from_env  ' }, () => 'jmt_from_file', '/home/tester')
  assert.equal(token, 'jmt_from_env')
})

test('the token file is used when the environment variable is absent', () => {
  const seen = []
  const token = resolveToken(CONFIG, {}, (file) => {
    seen.push(file)
    return 'jmt_from_file\n'
  }, '/home/tester')
  assert.equal(token, 'jmt_from_file')
  assert.deepEqual(seen, ['/home/tester/.jarvismind/dsh-agent.token'])
})

test('a missing token file degrades to undefined instead of throwing', () => {
  const token = resolveToken(CONFIG, {}, () => {
    throw new Error('ENOENT')
  }, '/home/tester')
  assert.equal(token, undefined)
})

test('an empty or whitespace token file degrades to undefined', () => {
  assert.equal(resolveToken(CONFIG, {}, () => '   \n', '/home/tester'), undefined)
})

test('~ expands against the provided home directory only at the start', () => {
  assert.equal(expandHome('~', '/home/tester'), '/home/tester')
  assert.equal(expandHome('~/a/b', '/home/tester'), '/home/tester/a/b')
  assert.equal(expandHome('/var/tmp/a', '/home/tester'), '/var/tmp/a')
  assert.ok(expandHome('relative/a', '/home/tester').endsWith('/relative/a'))
})