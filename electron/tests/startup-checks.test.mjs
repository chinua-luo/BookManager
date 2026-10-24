import assert from 'node:assert/strict'
import test from 'node:test'
import os from 'node:os'
import path from 'node:path'
import { mkdtemp, mkdir, writeFile, readdir, readFile, rm } from 'node:fs/promises'
import { checkFrontend, startupFailure } from '../startup-checks.mjs'
import { createMainLogger } from '../diagnostics.mjs'
import { notificationFromError } from '../user-errors.mjs'

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'bookmanager-startup-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  await mkdir(path.join(root, 'assets'), { recursive: true })
  await mkdir(path.join(root, 'vendor/djvu'), { recursive: true })
  await writeFile(path.join(root, 'index.html'), '<script type="module" src="/assets/main.js"></script><link href="/assets/main.css" rel="stylesheet">')
  for (const name of ['assets/main.js', 'assets/main.css', 'vendor/djvu/djvu.js', 'vendor/djvu/djvu_viewer.js']) await writeFile(path.join(root, name), 'fixture')
  return root
}

test('frontend preflight verifies the built entry and bundled preview dependencies', async t => {
  const root = await fixture(t)
  await checkFrontend(root)
  await rm(path.join(root, 'assets/main.js'))
  await assert.rejects(checkFrontend(root), error => notificationFromError(error).errorType === 'frontend_missing')
})

test('frontend preflight rejects missing DjVu assets and external or escaping entries', async t => {
  const root = await fixture(t)
  await rm(path.join(root, 'vendor/djvu/djvu.js'))
  await assert.rejects(checkFrontend(root), /BM_NOTICE/)
  for (const source of ['https://example.com/main.js', '../main.js']) {
    await writeFile(path.join(root, 'index.html'), `<script src="${source}"></script>`)
    await assert.rejects(checkFrontend(root), /BM_NOTICE/)
  }
})

test('fatal startup reasons have specific safe system messages', () => {
  for (const type of ['dependency_missing', 'python_missing', 'data_permission', 'data_corrupt', 'disk_full', 'frontend_missing']) {
    const notice = notificationFromError(startupFailure(type))
    assert.equal(notice.level, 'fatal'); assert.equal(notice.errorType, type)
    assert.doesNotMatch(notice.msg, /Traceback|ENOENT|EACCES/)
  }
  assert.equal(notificationFromError(startupFailure('Traceback secret')).errorType, 'startup_failed')
})

test('main process diagnostics persist Error stacks and redact tokens', async t => {
  const root = await fixture(t)
  const log = createMainLogger(root, { token: 'bridge-secret' })
  log('Request failed:', new Error('bridge-secret Bearer bearer-secret https://example.com/?token=url-secret'))
  const [name] = await readdir(path.join(root, 'logs'))
  const record = JSON.parse(await readFile(path.join(root, 'logs', name), 'utf8'))
  assert.match(record.detail, /Error:|at /)
  assert.doesNotMatch(record.detail, /bridge-secret|bearer-secret|url-secret/)
})

test('main process logging failure falls back once without recursion or throwing', async t => {
  const root = await fixture(t); const file = path.join(root, 'file')
  await writeFile(file, 'not a directory')
  const messages = []
  const log = createMainLogger(file, { fallback: value => messages.push(value) })
  assert.doesNotThrow(() => log(new Error('original failure')))
  assert.equal(messages.length, 1)
  assert.match(messages[0], /original failure/)
})
