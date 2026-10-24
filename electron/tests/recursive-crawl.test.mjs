import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { crawlRequestPayload, recursiveEnabled } from '../src/recursive-crawl.mjs'

const rendererSource = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')

test('recursive crawling requires both a checked option and explicit confirmation', () => {
  assert.equal(recursiveEnabled(false, false), false)
  assert.equal(recursiveEnabled(true, false), false)
  assert.equal(recursiveEnabled(false, true), false)
  assert.equal(recursiveEnabled(true, true), true)
})

test('crawl payload defaults to ordinary non-recursive behavior', () => {
  assert.deepEqual(crawlRequestPayload('https://example.com', 7, false, false), {
    source: 'https://example.com',
    folderId: 7,
    enable_recursive: false
  })
})

test('crawl dialog contains the risk gate and resets state for each opening', () => {
  assert.match(rendererSource, /高级：抓取页面内同站链接（有安全风险）/)
  assert.match(rendererSource, /modal\('风险提示'/)
  assert.match(rendererSource, /我已了解风险，确认开启/)
  assert.match(rendererSource, /let recursiveConfirmed = false/)
  assert.match(rendererSource, /recursiveConfirmed = await confirmRecursiveRisk\(\)/)
})
