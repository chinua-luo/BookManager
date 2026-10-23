import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { createDjvuPreview, isDjvuExtension } from '../src/djvu-preview.js'

const vendorDirectory = fileURLToPath(new URL('../src/public/vendor/djvu/', import.meta.url))

test('vendors the expected DjVu.js releases and license notice', async () => {
  const [library, viewer, license] = await Promise.all([
    readFile(`${vendorDirectory}/djvu.js`, 'utf8'),
    readFile(`${vendorDirectory}/djvu_viewer.js`, 'utf8'),
    readFile(`${vendorDirectory}/LICENSE.md`, 'utf8')
  ])
  assert.match(library, /VERSION: '0\.5\.4'/)
  assert.match(viewer, /VERSION","0\.10\.0"/)
  assert.match(license, /either Version 2 of the license/)
  assert.match(license, /or \(at your option\) any later version/)
})

test('recognizes both DJVU file extensions case-insensitively', () => {
  assert.equal(isDjvuExtension('.djvu'), true)
  assert.equal(isDjvuExtension('.DJV'), true)
  assert.equal(isDjvuExtension('.pdf'), false)
})

test('loads DJVU through the browser viewer in continuous mode', async () => {
  const calls = []
  class Viewer {
    render(host) { calls.push(['render', host]) }
    async loadDocumentByUrl(url, config) { calls.push(['load', url, config]) }
  }

  const host = { id: 'preview' }
  const preview = createDjvuPreview(host, { Viewer })
  await preview.load('bookmanager-document://content/1', 'Sample.djvu')

  assert.deepEqual(calls[0], ['render', host])
  assert.equal(calls[1][0], 'load')
  assert.equal(calls[1][1], 'bookmanager-document://content/1')
  assert.equal(calls[1][2].name, 'Sample.djvu')
  assert.equal(calls[1][2].viewMode, 'continuous')
  assert.equal(calls[1][2].uiOptions.hideOpenAndCloseButtons, true)
})

test('reuses one viewer instance when another DJVU is selected', async () => {
  let constructors = 0
  class Viewer {
    constructor() { constructors += 1 }
    render() {}
    async loadDocumentByUrl() {}
  }

  const preview = createDjvuPreview({}, { Viewer })
  await preview.load('/one.djvu', 'One.djvu')
  await preview.load('/two.djvu', 'Two.djvu')

  assert.equal(constructors, 1)
})

test('reports a useful error when the vendored viewer did not load', () => {
  const preview = createDjvuPreview({}, undefined)
  assert.rejects(preview.load('/book.djvu', 'Book.djvu'), /DJVU 预览组件没有正确加载/)
})
