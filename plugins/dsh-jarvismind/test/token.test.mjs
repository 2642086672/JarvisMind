import assert from 'node:assert/strict'
import test from 'node:test'
import { join } from 'node:path'
import { resolveToken, expandHome } from '../dist/token.js'

// The plugin ships for DSH Desktop on macOS and also runs on Windows. Path
// expectations must use the host platform's separators and absolute-path shape:
// `node:path` cannot produce a POSIX string on Windows, so hardcoded `/home/...`
// assertions failed there even though the runtime behaviour was correct.
const WIN = process.platform === 'win32'
const HOME = WIN ? 'C:\\Users\\example-user' : '/home/example-user'
const ABSOLUTE = WIN ? 'C:\\var\\tmp\\a' : '/var/tmp/a'

const CONFIG = { tokenEnv: 'JM_TEST_TOKEN', tokenFile: '~/.jarvismind/dsh-agent.token' }

test('the environment variable wins over the token file', () => {
  const token = resolveToken(CONFIG, { JM_TEST_TOKEN: '  jmt_from_env  ' }, () => 'jmt_from_file', HOME)
  assert.equal(token, 'jmt_from_env')
})

test('the token file is used when the environment variable is absent', () => {
  const seen = []
  const token = resolveToken(CONFIG, {}, (file) => {
    seen.push(file)
    return 'jmt_from_file\n'
  }, HOME)
  assert.equal(token, 'jmt_from_file')
  assert.deepEqual(seen, [join(HOME, '.jarvismind', 'dsh-agent.token')])
})

test('a missing token file degrades to undefined instead of throwing', () => {
  const token = resolveToken(CONFIG, {}, () => {
    throw new Error('ENOENT')
  }, HOME)
  assert.equal(token, undefined)
})

test('an empty or whitespace token file degrades to undefined', () => {
  assert.equal(resolveToken(CONFIG, {}, () => '   \n', HOME), undefined)
})

test('~ expands against the provided home directory only at the start', () => {
  assert.equal(expandHome('~', HOME), HOME)
  assert.equal(expandHome('~/a/b', HOME), join(HOME, 'a', 'b'))
  assert.equal(expandHome(ABSOLUTE, HOME), ABSOLUTE)
  assert.ok(expandHome('relative/a', HOME).endsWith(join('relative', 'a')))
})

test('a Windows backslash after ~ is still home-relative', { skip: !WIN }, () => {
  // Without this, `~\.jarvismind\dsh-agent.token` resolved against the process
  // working directory and the plugin silently ran without a token.
  assert.equal(expandHome('~\\a\\b', HOME), join(HOME, 'a', 'b'))
  assert.equal(
    expandHome('~\\.jarvismind\\dsh-agent.token', HOME),
    join(HOME, '.jarvismind', 'dsh-agent.token'),
  )
})
