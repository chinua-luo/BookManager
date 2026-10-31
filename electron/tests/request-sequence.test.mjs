import assert from 'node:assert/strict'
import test from 'node:test'

import { createRequestSequence } from '../src/request-sequence.js'

test('only the newest request token is considered current', () => {
  const sequence = createRequestSequence()
  const first = sequence.begin()
  const second = sequence.begin()
  assert.equal(typeof first, 'number')
  assert.equal(sequence.isCurrent(first), false)
  assert.equal(sequence.isCurrent(second), true)
})

test('a superseded response cannot become current again', () => {
  const sequence = createRequestSequence()
  const token = sequence.begin()
  sequence.begin()
  sequence.invalidate()
  assert.equal(sequence.isCurrent(token), false)
})

test('independent sequences do not invalidate each other', () => {
  const folders = createRequestSequence()
  const refreshes = createRequestSequence()
  const folderToken = folders.begin()
  const refreshToken = refreshes.begin()
  folders.begin()
  assert.equal(folders.isCurrent(folderToken), false)
  assert.equal(refreshes.isCurrent(refreshToken), true)
})
