import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const mainSource = await readFile(new URL('../main.mjs', import.meta.url), 'utf8')
const rendererSource = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')

test('browser window keeps Electron isolation and blocks external navigation', () => {
  assert.match(mainSource, /contextIsolation:\s*true/)
  assert.match(mainSource, /nodeIntegration:\s*false/)
  assert.match(mainSource, /sandbox:\s*true/)
  assert.match(mainSource, /setWindowOpenHandler\(\(\) => \(\{ action: 'deny' \}\)\)/)
  assert.match(mainSource, /will-navigate/)
})

test('preview transport does not put the bridge token in a URL', () => {
  assert.doesNotMatch(mainSource, /content\?token=/)
  assert.match(mainSource, /documentUrl\(documentId\)/)
  assert.match(mainSource, /corsEnabled:\s*true/)
})

test('modal and user-visible rows use DOM text nodes rather than raw user HTML', () => {
  assert.match(rendererSource, /body\.append\(content\)/)
  assert.match(rendererSource, /element\('span', '', item\.listName\)/)
  assert.match(rendererSource, /element\('span', '', folder\.name\)/)
  assert.match(rendererSource, /element\('span', '', entry\.item\.displayName\)/)
})
