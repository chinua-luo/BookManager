import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

import { createViewerLoader } from '../src/viewer-loader.js'

const main = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')
const loader = await readFile(new URL('../src/viewer-loader.js', import.meta.url), 'utf8')

function section(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker)
  const end = start < 0 ? -1 : source.indexOf(endMarker, start)
  assert.ok(start >= 0 && end > start, `missing section ${startMarker} -> ${endMarker}`)
  return source.slice(start, end)
}

test('viewer and renderers are imported once and shared by concurrent callers', async () => {
  let viewerImports = 0
  let rendererImports = 0
  const fakeRenderers = [{ id: 'pdf' }, { id: 'text' }]
  const instance = createViewerLoader({
    importViewer: async () => { viewerImports += 1; return { mountViewer: () => ({ mounted: true }) } },
    importRenderers: async () => { rendererImports += 1; return fakeRenderers }
  })
  const [first, second] = await Promise.all([instance.load(), instance.load()])
  assert.equal(viewerImports, 1)
  assert.equal(rendererImports, 1)
  assert.equal(first, second)
  assert.deepEqual(first.renderers, fakeRenderers)
  assert.equal(typeof first.mountViewer, 'function')
  assert.equal(await instance.load(), first)
})

test('a failed lazy import can be retried without poisoning the cache', async () => {
  let attempts = 0
  const instance = createViewerLoader({
    importViewer: async () => { attempts += 1; if (attempts === 1) throw new Error('chunk failed'); return { mountViewer: () => ({}) } },
    importRenderers: async () => []
  })
  await assert.rejects(instance.load(), /chunk failed/)
  const recovered = await instance.load()
  assert.equal(attempts, 2)
  assert.deepEqual(recovered.renderers, [])
})

test('main.js has no static viewer/renderer imports and mounts the viewer lazily', () => {
  assert.doesNotMatch(main, /@file-viewer\//)
  assert.match(main, /import \{ viewerLoader \} from '\.\/viewer-loader\.js'/)
  assert.match(main, /let viewer = null/)
  assert.match(main, /function ensureViewer\(\)/)
  assert.match(main, /viewerLoader\.load\(\)\.then\(\(\{ mountViewer, renderers \}\) =>/)
  assert.match(main, /mountViewer\(viewerHost, \{ options: \{ \.\.\.viewerOptions, renderers \} \}\)/)
  assert.match(main, /viewer\?\.getApi\?\.\(\)/)
})

test('text and DJVU previews never reach the lazy viewer loader', () => {
  const load = section(main, 'const previewCoordinator = createPreviewCoordinator', 'function folderSummary(')
  const text = load.indexOf('textExtensions.has(extension)')
  const djvu = load.indexOf('isDjvuExtension(extension)')
  const ensure = load.indexOf('ensureViewer()')
  assert.ok(text >= 0 && djvu > text && ensure > djvu, 'ensureViewer must only appear in the fallback viewer branch')
  assert.equal((load.match(/ensureViewer\(\)/g) || []).length, 1)
  assert.match(load, /const activeViewer = await ensureViewer\(\)/)
  assert.match(load, /await activeViewer\.load\(/)
})

test('viewer-loader keeps every renderer behind a dynamic import', () => {
  const packages = ['web', 'renderer-pdf', 'renderer-word', 'renderer-spreadsheet', 'renderer-pptx', 'renderer-epub', 'renderer-image', 'renderer-text']
  for (const name of packages) assert.match(loader, new RegExp(`import\\('@file-viewer/${name}'\\)`), name)
  assert.doesNotMatch(loader, /^import .*@file-viewer/m)
})
