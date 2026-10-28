import assert from 'node:assert/strict'
import test from 'node:test'

import { allowedBridgePath, assertAllowedBridgeRequest } from '../ipc-policy.mjs'

test('renderer bridge paths are explicitly allowlisted', () => {
  assert.equal(allowedBridgePath('GET', '/api/tree'), true)
  assert.equal(allowedBridgePath('GET', '/api/folders/12/contents'), true)
  assert.equal(allowedBridgePath('POST', '/api/items/9/rename'), true)
  assert.doesNotThrow(() => assertAllowedBridgeRequest('POST', '/api/name-validate', { nameParts: { subtitle: 'One - Two' } }))
  assert.equal(allowedBridgePath('GET', '/api/name-validate'), false)
  assert.equal(allowedBridgePath('POST', '/api/name-validate?bypass=true'), false)
  assert.equal(allowedBridgePath('POST', '/api/shutdown'), false)
  assert.equal(allowedBridgePath('GET', '/api/tree?token=secret'), false)
  assert.equal(allowedBridgePath('POST', '/../api/search'), false)
})

test('oversized renderer payloads are rejected', () => {
  assert.doesNotThrow(() => assertAllowedBridgeRequest('POST', '/api/search', { query: 'book' }))
  assert.throws(() => assertAllowedBridgeRequest('POST', '/api/search', { query: 'x'.repeat(1024 * 1024) }))
})
