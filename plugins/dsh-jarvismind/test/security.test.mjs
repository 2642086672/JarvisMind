import assert from 'node:assert/strict'
import test from 'node:test'
import { publicUrl, redactSecrets, safeErrorMessage } from '../dist/security.js'

test('token-shaped text is redacted', () => {
  assert.equal(redactSecrets('prefix jmt_abc123 suffix'), 'prefix jmt_[redacted] suffix')
  assert.equal(redactSecrets('no token here'), 'no token here')
})

test('safeErrorMessage redacts thrown strings and errors', () => {
  assert.equal(safeErrorMessage(new Error('bad jmt_abc123')), 'bad jmt_[redacted]')
  assert.equal(safeErrorMessage('bad jmt_abc123'), 'bad jmt_[redacted]')
  assert.equal(safeErrorMessage(undefined), 'unknown error')
})

test('publicUrl strips URL credentials before logging', () => {
  assert.equal(publicUrl('http://user:pass@127.0.0.1:8777/'), 'http://127.0.0.1:8777')
  assert.equal(publicUrl('http://127.0.0.1:8777/api/'), 'http://127.0.0.1:8777/api')
  assert.equal(publicUrl('not a url'), 'invalid-url')
})