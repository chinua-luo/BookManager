import assert from 'node:assert/strict'
import test from 'node:test'
import { createModalManager } from '../src/modal-manager.js'
import { userError, businessError, notificationFromError, importSummary, crawlSummary, createFatalReporter } from '../user-errors.mjs'

function harness() {
  const ticks = []; const visible = new Set(); const events = []; let clock = 0
  const manager = createModalManager({ now: () => clock, defer: fn => ticks.push(fn), log: text => events.push(text) })
  return {
    manager, visible, events,
    tick() { const batch = ticks.splice(0); batch.forEach(fn => fn()) },
    time(value) { clock = value },
    entry(name, allowChild = false) { return { allowChild, show() { visible.add(name); assert.equal(visible.size, 1) }, hide() { visible.delete(name) }, dismiss() { visible.delete(name); events.push(`dismiss:${name}`) } } }
  }
}

test('dialogs drain FIFO on later event loops without reapplying dedup', () => {
  const h = harness()
  const close = h.manager.show(h.entry('first'))
  const closeSecond = h.manager.show(h.entry('second'), { key: 'failure', dedup: true })
  const closeThird = h.manager.show(h.entry('third'))
  assert.deepEqual([...h.visible], ['first'])
  close(); assert.equal(h.visible.size, 0)
  h.tick(); assert.deepEqual([...h.visible], ['second'])
  closeSecond(); h.tick(); assert.deepEqual([...h.visible], ['third'])
  closeThird(); h.tick(); assert.equal(h.visible.size, 0)
})

test('five rapid delete requests create one confirmation and cancel duplicates', () => {
  const h = harness()
  const close = h.manager.show(h.entry('delete'), { key: 'delete:42' })
  for (let i = 0; i < 4; i++) h.manager.show(h.entry(`duplicate${i}`), { key: 'delete:42' })
  assert.equal(h.events.filter(value => value.startsWith('dismiss:duplicate')).length, 4)
  close(); h.tick(); assert.equal(h.visible.size, 0)
})

test('same error is suppressed for three seconds and while still pending', () => {
  const h = harness()
  const close = h.manager.show(h.entry('one'), { key: 'import', dedup: true })
  h.time(5000); h.manager.show(h.entry('pending'), { key: 'import', dedup: true })
  assert.deepEqual([...h.visible], ['one'])
  close(); h.tick()
  const closeAgain = h.manager.show(h.entry('two'), { key: 'import', dedup: true })
  closeAgain(); h.tick(); h.time(7999)
  h.manager.show(h.entry('suppressed'), { key: 'import', dedup: true })
  assert.equal(h.visible.size, 0)
  h.time(8000); h.manager.show(h.entry('three'), { key: 'import', dedup: true })
  assert.deepEqual([...h.visible], ['three'])
})

test('child confirmation hides and restores form; errors wait behind confirmations', () => {
  const h = harness()
  const closeParent = h.manager.show(h.entry('form', true))
  const closeChild = h.manager.show(h.entry('confirm'), { nested: true })
  const closeError = h.manager.show(h.entry('error'), { nested: true })
  assert.deepEqual([...h.visible], ['confirm'])
  closeChild(); h.tick(); assert.deepEqual([...h.visible], ['form'])
  closeParent(); h.tick(); assert.deepEqual([...h.visible], ['error'])
  closeError(); h.tick()
})

test('reentrant dismissal waits for the next event loop and closing is idempotent', () => {
  const h = harness()
  const entry = h.entry('first'); const dismiss = entry.dismiss
  entry.dismiss = () => { dismiss(); h.manager.show(h.entry('callback')) }
  const close = h.manager.show(entry)
  close(); close(); assert.equal(h.visible.size, 0)
  h.tick(); assert.deepEqual([...h.visible], ['callback'])
})

test('raw exceptions and IPC decorations become readable business messages', () => {
  const raw = 'Traceback (most recent call last):\nRuntimeError: secret /Users/private/library.db'
  for (const context of ['/api/search', '/api/import/file', '/api/settings/cache/clear', '/api/settings/data/migrate', '/api/crawl']) {
    const encoded = businessError(raw, context)
    const notice = notificationFromError(new Error(`Error invoking remote method: Error: ${encoded.message}`))
    assert.ok(notice.msg.length > 10)
    assert.doesNotMatch(notice.msg, /Traceback|RuntimeError|secret|library.db/)
    assert.equal(notice.level, 'error')
  }
  assert.equal(userError('private network blocked').errorType, 'unsafe_address')
  assert.equal(userError('ENOSPC').errorType, 'disk_full')
  assert.equal(notificationFromError('BM_NOTICE:broken').title, '操作失败')
})

test('ten-file import with five failures produces a single warning with no raw details', () => {
  const warning = importSummary({ fileCount: 5, errors: Array(5).fill('Traceback: secret') })
  assert.equal(warning.level, 'warning')
  assert.match(warning.msg, /共 10 个文件，成功导入 5 个，5 个/)
  assert.doesNotMatch(warning.msg, /Traceback|secret/)
  assert.equal(importSummary({ fileCount: 10, errors: [] }), null)
})

test('fatal errors suppress repeats for ten seconds using the same backend key', () => {
  let time = 0; const messages = []
  const report = createFatalReporter((...args) => messages.push(args), () => time)
  assert.equal(report('backend', '程序异常', '后端服务意外退出，请重启软件'), true)
  time = 9999; assert.equal(report('backend', '程序异常', '连接失败'), false)
  time = 10000; assert.equal(report('backend', '程序异常', '连接失败'), true)
  assert.equal(messages.length, 2)
})

// Exercise the production DOM adapter and promise helpers, including dismissal.
import { readFile } from 'node:fs/promises'
const renderer = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')
function domHarness() {
  const h = harness()
  class Node {
    children = []; listeners = new Map(); hidden = false; isConnected = true; className = ''; disabled = false
    classList = { add() {}, remove() {} }
    append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node) } }
    remove() { this.isConnected = false; if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this) }
    setAttribute() {}
    addEventListener(type, fn) { const list = this.listeners.get(type) || []; list.push(fn); this.listeners.set(type, list) }
    querySelectorAll(selector) { return this.children.flatMap(node => [...(selector === 'button' && node.tag === 'button' ? [node] : []), ...node.querySelectorAll(selector)]) }
    querySelector(selector) { return this.querySelectorAll('button').find(node => selector === '.primary' ? node.className.includes('primary') : true) }
    focus() {}
    async click() { if (!this.disabled) for (const fn of this.listeners.get('click') || []) await fn({}) }
  }
  const document = { createElement(tag) { const node = new Node(); node.tag = tag; return node }, activeElement: null }
  const root = new Node(); const failures = []
  const start = renderer.indexOf('function modal('); const end = renderer.indexOf('function tagValues(', start)
  const make = new Function('document', 'modalRoot', 'modalManager', 'element', 'error', `${renderer.slice(start, end)}; return { modal, ask, promptValue }`)
  const element = (tag, className, text) => { const node = document.createElement(tag); node.className = className; node.textContent = text; return node }
  const adapter = make(document, root, h.manager, element, issue => failures.push(issue))
  return { ...h, ...adapter, document, root, failures }
}

test('DOM confirmation duplicate resolves false and original confirm resolves true once', async () => {
  const h = domHarness()
  const first = h.ask('确认删除', '确定删除？', true)
  const duplicate = h.ask('确认删除', '确定删除？', true)
  assert.equal(await duplicate, false)
  const buttons = h.root.querySelectorAll('button')
  await buttons.find(button => button.textContent === '确认').click()
  assert.equal(await first, true)
  assert.equal(h.root.children.length, 0)
})

test('DOM prompt returns entered text and backdrop dismissal cancels', async () => {
  const h = domHarness()
  const result = h.promptValue('名称', '请输入', '新文件夹')
  await h.root.querySelector('.primary').click()
  assert.equal(await result, '新文件夹')
  h.tick()
  const cancelled = h.promptValue('名称', '请输入')
  const cover = h.root.children[0]
  for (const fn of cover.listeners.get('mousedown')) fn({ target: cover })
  assert.equal(await cancelled, null)
})

test('DOM async button errors are caught and rapid clicks cannot repeat mutations', async () => {
  const h = domHarness(); let finish; let calls = 0
  const wait = new Promise(resolve => { finish = resolve })
  const issue = new Error('save failed')
  const dialog = h.modal('设置', h.document.createElement('p'), { buttons: [{ label: '保存', primary: true, action: async () => { calls++; await wait; throw issue } }] })
  const button = dialog.panel.querySelector('.primary')
  const pending = button.click(); await button.click(); assert.equal(calls, 1)
  finish(); await pending
  assert.deepEqual(h.failures, [issue]); assert.equal(button.disabled, false)
})


test('crawl warnings aggregate raw failures and explicitly identify HTML fallback', () => {
  assert.equal(crawlSummary({ downloaded: 3 }), null)
  const partial = crawlSummary({ downloaded: 2, stats: { skipped_cross_origin: 3 }, messages: ['跳过 https://private: Traceback secret'] })
  assert.equal(partial.level, 'warning'); assert.match(partial.msg, /3 个/)
  assert.doesNotMatch(partial.msg, /private|Traceback|secret/)
  const fallback = crawlSummary({ downloaded: 1, messages: ['所有下载链接均失败，已保存源页面 HTML。'] })
  assert.equal(fallback.title, '未能下载文档'); assert.match(fallback.msg, /已保存源页面/)
  assert.equal(importSummary({ fileCount: 0, errors: ['broken'] }).level, 'error')
  assert.equal(notificationFromError(businessError('fetch failed', '/api/tree', true)).level, 'fatal')
})

import { EventEmitter } from 'node:events'
const main = await readFile(new URL('../main.mjs', import.meta.url), 'utf8')
function backendHarness() {
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter()
  const reports = []
  const source = main.slice(main.indexOf('function startBridge('), main.indexOf('async function responseJson('))
  const create = new Function('spawn', 'pythonCommand', 'reportFatal', `
    let bridge = null; let bridgeUrl = null; let quitting = false;
    const staticDir = '/tmp'; const projectDir = '/tmp'; const token = 'test-token';
    ${source}
    return { startBridge, quit() { quitting = true } }
  `)
  return { child, reports, ...create(() => child, () => ({ executable: 'python3', prefix: [] }), (...args) => reports.push(args)) }
}

test('production backend lifecycle reports unexpected exit but suppresses normal shutdown', async () => {
  for (const normalShutdown of [false, true]) {
    const h = backendHarness(); const ready = h.startBridge()
    h.child.stdout.emit('data', Buffer.from('{"event":"ready","port":12345}\n'))
    await ready
    if (normalShutdown) h.quit()
    h.child.emit('exit', 1)
    assert.equal(h.reports.length, normalShutdown ? 0 : 1)
    if (!normalShutdown) assert.equal(h.reports[0][2], '后端服务意外退出，请重启软件')
  }
})

test('production backend lifecycle rejects missing Python without leaking details to dialogs', async () => {
  const h = backendHarness(); const ready = h.startBridge()
  h.child.emit('error', new Error('ENOENT python3'))
  await assert.rejects(ready, /ENOENT/)
  assert.equal(h.reports.length, 0)
})

test('production response boundary handles old and new error shapes and invalid JSON', async () => {
  const source = main.slice(main.indexOf('async function responseJson('), main.indexOf('async function readJson('))
  const responseJson = new Function('businessError', 'console', `${source}; return responseJson`)(businessError, { error() {} })
  for (const [payload, status] of [[{ error: 'Traceback: secret' }, 400], [{ ok: false, msg: 'private network' }, 200]]) {
    await assert.rejects(responseJson(new Response(JSON.stringify(payload), { status }), '/api/crawl'), error => {
      assert.doesNotMatch(notificationFromError(error).msg, /Traceback|secret/)
      return true
    })
  }
  await assert.rejects(responseJson(new Response('invalid JSON'), '/api/search'), /BM_NOTICE/)
  assert.deepEqual(await responseJson(new Response('{"folderId":3}'), '/api/import/folder'), { folderId: 3 })
})


test('optional tag prompt distinguishes an empty submission from cancellation', async () => {
  const h = domHarness()
  const empty = h.promptValue('标签', '可留空', '', true)
  await h.root.querySelector('.primary').click()
  assert.equal(await empty, '')
  h.tick()
  const cancelled = h.promptValue('标签', '可留空', '', true)
  const cover = h.root.children[0]
  for (const fn of cover.listeners.get('mousedown')) fn({ target: cover })
  assert.equal(await cancelled, null)
})
