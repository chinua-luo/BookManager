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
  assert.equal(allowedBridgePath('GET', '/api/import/requests/request-1'), true)
  assert.equal(allowedBridgePath('POST', '/api/import/requests/request-1'), false)
  assert.equal(allowedBridgePath('GET', '/api/import/requests/../settings'), false)
  assert.equal(allowedBridgePath('GET', '/api/import/folder/progress/job-1'), true)
  assert.equal(allowedBridgePath('GET', '/api/import/folder/progress/job_1-x'), true)
  assert.equal(allowedBridgePath('GET', '/api/import/folder/progress/'), false)
  assert.equal(allowedBridgePath('GET', '/api/import/folder/progress/../settings'), false)
  assert.equal(allowedBridgePath('POST', '/api/import/folder/progress/job-1'), false)
  assert.equal(allowedBridgePath('POST', '/api/import/folder/cancel/job-1'), true)
  assert.equal(allowedBridgePath('POST', '/api/import/folder/cancel/'), false)
  assert.equal(allowedBridgePath('POST', '/api/import/folder/cancel/job-1/../x'), false)
  assert.equal(allowedBridgePath('GET', '/api/import/folder/cancel/job-1'), false)
  assert.equal(allowedBridgePath('POST', '/api/folders/12/export'), true)
  assert.doesNotThrow(() => assertAllowedBridgeRequest('POST', '/api/folders/12/export', { targetPath: '/Users/snow/Desktop' }))
  assert.equal(allowedBridgePath('POST', '/api/folders/12/export?target=/tmp'), false)
  assert.equal(allowedBridgePath('POST', '/api/folders/12/export/extra'), false)
  assert.equal(allowedBridgePath('POST', '/api/folders/12/export#top'), false)
  assert.equal(allowedBridgePath('POST', '/api/folders/12/export/../delete'), false)
  assert.equal(allowedBridgePath('GET', '/api/folders/12/export'), false)
  assert.equal(allowedBridgePath('POST', '/api/folders/abc/export'), false)
  assert.equal(allowedBridgePath('POST', '/api/shutdown'), false)
  assert.equal(allowedBridgePath('GET', '/api/tree?token=secret'), false)
  assert.equal(allowedBridgePath('POST', '/../api/search'), false)
})

test('main-process-only local item paths stay out of the renderer allowlist', () => {
  for (const path of ['/api/items/9/open', '/api/items/9/location']) {
    assert.equal(allowedBridgePath('GET', path), false)
    assert.equal(allowedBridgePath('POST', path), false)
    assert.throws(() => assertAllowedBridgeRequest('GET', path), TypeError)
  }
  // Settings deliberately exposes dataHome to the renderer.
  assert.equal(allowedBridgePath('GET', '/api/settings'), true)
  assert.doesNotThrow(() => assertAllowedBridgeRequest('GET', '/api/settings'))
  // Export receipts deliberately expose the chosen targetPath to the renderer.
  assert.doesNotThrow(() => assertAllowedBridgeRequest('POST', '/api/folders/12/export', { targetPath: '/tmp/export' }))
})

test('oversized renderer payloads are rejected', () => {
  assert.doesNotThrow(() => assertAllowedBridgeRequest('POST', '/api/search', { query: 'book' }))
  assert.doesNotThrow(() => assertAllowedBridgeRequest('POST', '/api/import/folder/cancel/job-1', {}))
  assert.doesNotThrow(() => assertAllowedBridgeRequest('GET', '/api/import/folder/progress/job-1'))
  assert.throws(() => assertAllowedBridgeRequest('POST', '/api/search', { query: 'x'.repeat(1024 * 1024) }))
})
