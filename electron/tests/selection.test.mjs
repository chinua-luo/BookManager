import assert from 'node:assert/strict'
import test from 'node:test'

import { isNavigationKey, nextRowIndex } from '../src/selection.js'

test('down and up move one row and clamp at the list edges', () => {
  assert.equal(nextRowIndex(3, 0, 'ArrowDown'), 1)
  assert.equal(nextRowIndex(3, 1, 'ArrowUp'), 0)
  assert.equal(nextRowIndex(3, 2, 'ArrowDown'), 2)
  assert.equal(nextRowIndex(3, 0, 'ArrowUp'), 0)
})

test('navigation without an active row starts at a sensible edge', () => {
  assert.equal(nextRowIndex(4, -1, 'ArrowDown'), 0)
  assert.equal(nextRowIndex(4, -1, 'ArrowUp'), 3)
  assert.equal(nextRowIndex(4, 9, 'ArrowDown'), 0)
  assert.equal(nextRowIndex(4, 9, 'ArrowUp'), 3)
})

test('empty lists and unrelated keys do not move focus', () => {
  assert.equal(nextRowIndex(0, 0, 'ArrowDown'), -1)
  assert.equal(nextRowIndex(-1, 0, 'ArrowUp'), -1)
  assert.equal(nextRowIndex(3, 1, 'ArrowLeft'), 1)
  assert.equal(nextRowIndex(3, 1, 'Enter'), 1)
  assert.equal(nextRowIndex(3, 9, 'Enter'), -1)
})

test('only arrow keys are treated as row navigation', () => {
  assert.equal(isNavigationKey('ArrowUp'), true)
  assert.equal(isNavigationKey('ArrowDown'), true)
  assert.equal(isNavigationKey('Enter'), false)
  assert.equal(isNavigationKey(' '), false)
  assert.equal(isNavigationKey('ArrowLeft'), false)
})
