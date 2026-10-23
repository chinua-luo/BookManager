import assert from 'node:assert/strict'
import { test } from 'node:test'

import { readDoubleClickDelayMs, resolveDoubleClickDelayMs } from '../double-click.mjs'

test('macOS double-click preference is converted to milliseconds with a safety margin', () => {
  const preferences = { getUserDefault: () => 0.5 }
  assert.equal(readDoubleClickDelayMs(preferences, 'darwin'), 600)
})

test('missing or invalid preferences use the safe fallback', () => {
  assert.equal(resolveDoubleClickDelayMs(null), 600)
  assert.equal(readDoubleClickDelayMs({ getUserDefault: () => { throw new Error('missing') } }, 'darwin'), 600)
})

test('unreasonable system values are bounded', () => {
  assert.equal(resolveDoubleClickDelayMs(0.01), 350)
  assert.equal(resolveDoubleClickDelayMs(10), 1500)
})
