import assert from 'node:assert/strict'
import test from 'node:test'
import {
  TEXT_PREVIEW_LIMIT_BYTES,
  formatPreviewBytes,
  textPreviewNotice,
  textPreviewRequest
} from '../src/text-preview.js'

test('small and unknown text files keep the full-read path', () => {
  assert.equal(TEXT_PREVIEW_LIMIT_BYTES, 2 * 1024 * 1024)
  for (const size of [0, 1, 1024, TEXT_PREVIEW_LIMIT_BYTES, undefined, null, Number.NaN, 'nope']) {
    assert.deepEqual(textPreviewRequest(size), { range: null, init: {} })
  }
})

test('large text files request only the first 2 MiB through a Range header', () => {
  const request = textPreviewRequest(TEXT_PREVIEW_LIMIT_BYTES + 1)
  assert.deepEqual(request.range, { start: 0, end: TEXT_PREVIEW_LIMIT_BYTES - 1, total: TEXT_PREVIEW_LIMIT_BYTES + 1 })
  assert.deepEqual(request.init, { headers: { Range: `bytes=0-${TEXT_PREVIEW_LIMIT_BYTES - 1}` } })
  assert.equal(textPreviewRequest('104857600').range.total, 104857600)
})

test('custom limits are validated', () => {
  assert.deepEqual(textPreviewRequest(10, 5).init, { headers: { Range: 'bytes=0-4' } })
  for (const limit of [0, -1, 1.5, '10', Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => textPreviewRequest(10, limit), TypeError)
  }
})

test('truncation notice reports total and shown sizes', () => {
  assert.equal(textPreviewNotice(null), '')
  const request = textPreviewRequest(10 * 1024 * 1024)
  const notice = textPreviewNotice(request.range)
  assert.match(notice, /文件较大/)
  assert.match(notice, /10 MB/)
  assert.match(notice, /2\.0 MB/)
  assert.equal(formatPreviewBytes(1536), '1.5 KB')
  assert.equal(formatPreviewBytes(512), '512 B')
})
