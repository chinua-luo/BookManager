import assert from 'node:assert/strict'
import test from 'node:test'

import { createDocumentProtocolHandler, documentIdFromUrl, documentUrl } from '../document-protocol.mjs'

test('document URLs expose only an opaque document id and never a token', () => {
  const url = documentUrl(42)
  assert.equal(url, 'bookmanager-document://content/42')
  assert.equal(documentIdFromUrl(url), 42)
  assert.equal(url.includes('token'), false)
})

test('invalid document URLs and ids are rejected', () => {
  assert.throws(() => documentUrl('../1'))
  assert.throws(() => documentIdFromUrl('bookmanager-document://other/1'))
  assert.throws(() => documentIdFromUrl('https://content/1'))
})

test('protocol handler forwards only the id and range header', async () => {
  const calls = []
  const handler = createDocumentProtocolHandler((id, range) => { calls.push([id, range]); return 'response' })
  const request = { url: documentUrl(7), headers: new Headers({ Range: 'bytes=10-20' }) }
  assert.equal(await handler(request), 'response')
  assert.deepEqual(calls, [[7, 'bytes=10-20']])
})
