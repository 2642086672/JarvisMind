import assert from 'node:assert/strict'
import test from 'node:test'
import { memoryGuide } from '../dist/memory-style.js'

/**
 * These are prompt texts, so the assertions are about *properties* the model
 * relies on, not about exact wording: a style must name the signals it is
 * looking for, must forbid credentials, and the companion style must ask for
 * silence instead of narrating the save.
 */

test('both styles forbid credentials explicitly', () => {
  for (const style of ['assistant', 'companion']) {
    const guide = memoryGuide(style)
    assert.match(guide, /password|credential/i, `${style} must forbid credentials`)
    assert.match(guide, /token|key/i, `${style} must forbid tokens and keys`)
  }
})

test('the assistant style names the durable signals it wants', () => {
  const guide = memoryGuide('assistant')
  assert.match(guide, /addressed/i)
  assert.match(guide, /preferences/i)
  assert.match(guide, /decisions/i)
  assert.match(guide, /dates/i)
})

test('the assistant style excludes the noise that filled the queue before', () => {
  const guide = memoryGuide('assistant')
  // The acknowledgements the old turn/end capture turned into memories.
  assert.match(guide, /嗯/)
  assert.match(guide, /谢谢/)
  assert.match(guide, /small talk/i)
})

test('the companion style asks the model to record silently', () => {
  const guide = memoryGuide('companion')
  assert.match(guide, /silently/i)
  assert.match(guide, /do not announce/i)
  // The specific phrasing that would break the moment being described.
  assert.match(guide, /I have saved this/)
})

test('the companion style looks for relationship, not only preferences', () => {
  const guide = memoryGuide('companion')
  assert.match(guide, /joke|reference/i)
  assert.match(guide, /emotional/i)
  assert.match(guide, /promise|agreed/i)
})

test('an unknown style falls back to the assistant guide', () => {
  assert.equal(memoryGuide('nonsense'), memoryGuide('assistant'))
  assert.equal(memoryGuide(undefined), memoryGuide('assistant'))
})
