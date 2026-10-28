import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { folderTargets, MAX_IMPORT_TARGETS } from '../src/folder-targets.js'
import { createImportController } from '../src/import-controller.js'
import { submitNameForm } from '../src/name-validation.js'
import { businessError, notificationFromError } from '../user-errors.mjs'
import { assertAllowedBridgeRequest } from '../ipc-policy.mjs'

const renderer = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')
const folders = [{ id: 1, name: 'Library', children: [{ id: 2, name: 'Math', children: [{ id: 3, name: 'Calculus', children: [] }] }] }]
const success = { requestId: 'request-1', entries: [{ folderId: 2, itemId: 1 }, { folderId: 3, itemId: 2 }], stats: { targetCount: 2, created: 2, existing: 0 }, automatic: [] }
function harness({ fail = null } = {}) {
  const dialogs = []; const sent = []; const saved = new Map(); let currentTree = structuredClone(folders)
  let focused; let askCount = 0
  class Node {
    classList = { add() {}, remove() {} }
    children = []; listeners = new Map(); attributes = new Map(); value = ''; disabled = false; isConnected = true; className = ''; checked = false
    append(...nodes) { this.children.push(...nodes); if (this.tag === 'select' && !this.value) this.value = nodes[0]?.text || '' }
    replaceChildren(...nodes) { this.children = [...nodes] }
    setAttribute(key, value) { this.attributes.set(key, value) }
    getAttribute(key) { return this.attributes.get(key) }
    removeAttribute(key) { this.attributes.delete(key) }
    addEventListener(key, value) { this.listeners.set(key, value) }
    querySelectorAll(selector) { const tags = selector.split(','); return this.children.flatMap(node => [...(tags.includes(node.tag) || (selector === '.primary' && node.className.includes('primary')) ? [node] : []), ...(node.querySelectorAll?.(selector) || [])]) }
    querySelector(selector) { return this.querySelectorAll(selector)[0] }
    focus() { if (!this.disabled) focused = this }
    async click() { if (!this.disabled) await this.listeners.get('click')?.({ stopPropagation() {} }) }
    async change(checked) { this.checked = checked; await this.listeners.get('change')?.({}) }
  }
  const document = { createElement: tag => Object.assign(new Node(), { tag }) }
  const element = (tag, className, textContent) => Object.assign(document.createElement(tag), { className, textContent })
  const api = {
    tree: async () => ({ folders: currentTree, libraryId: 'library' }),
    get: async path => { assertAllowedBridgeRequest('GET', path); return { state: 'pending' } },
    post: async (path, payload) => {
      assertAllowedBridgeRequest('POST', path, payload)
      if (path === '/api/name-candidate') return { nameParts: { mainTitle: 'Book', subtitle: '', extension: '.txt' } }
      if (path === '/api/name-metadata') return { extracted: false }
      if (path === '/api/name-validate') return payload.nameParts.subtitle.includes(' - ') ? { valid: false, field: 'subtitle', message: '副标题名包含保留分隔符' } : { valid: true }
      sent.push({ path, payload })
      if (fail) throw businessError({ error_type: fail, msg: 'specific failure', level: 'error' })
      return success
    }
  }
  const controller = createImportController({ api, storage: { getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) }, libraryId: 'library', newId: () => 'request-1' })
  const modal = (_title, content, options) => {
    const primary = element('button', 'primary'); const panel = element('section'); panel.append(primary)
    const dialog = { content, options, panel, closed: false }
    dialog.close = () => { if (options.canDismiss && !options.canDismiss()) return; dialog.closed = true; options.onDismiss?.() }
    dialog.submit = () => options.buttons.find(button => button.primary).action(dialog.close)
    dialogs.push(dialog); return dialog
  }
  const start = renderer.indexOf('async function readImportTree(')
  const end = renderer.indexOf('\nasync function showItem(', start)
  assert.ok(start >= 0 && end > start)
  const make = new Function('window', 'document', 'Option', 'modal', 'element', 'submitNameForm', 'folderTargets', 'MAX_IMPORT_TARGETS', 'notice', 'ask', 'notificationFromError', 'businessError', `let tree = ${JSON.stringify(folders)}; let selectedFolderId = 2; let libraryId = 'library'; ${renderer.slice(start, end)}; return { nameDialog, chooseImportFolders }`)
  const functions = make({ bookManager: api }, document, class { constructor(text) { this.text = text } }, modal, element, submitNameForm, folderTargets, MAX_IMPORT_TARGETS, () => {}, async () => { askCount++; return true }, notificationFromError, businessError)
  return { ...functions, controller, dialogs, sent, saved, focused: () => focused, askCount: () => askCount, setTree: value => { currentTree = value } }
}

test('folder chooser presents one labelled checkbox and complete path per flat row', async () => {
  const h = harness()
  const chosen = h.chooseImportFolders([2])
  const selector = h.dialogs[0]
  assert.equal(selector.content.querySelectorAll('details,summary').length, 0)
  const rows = selector.content.querySelectorAll('label')
  assert.equal(rows.length, 3)
  assert.deepEqual(rows.map(row => row.children.map(node => node.tag)), [['input', 'span'], ['input', 'span'], ['input', 'span']])
  const paths = ['Library', 'Library / Math', 'Library / Math / Calculus']
  assert.deepEqual(rows.map(row => row.children[1].textContent), paths)
  assert.deepEqual(rows.map(row => row.title), paths)
  assert.deepEqual(rows.map(row => row.children[0].attributes.get('aria-label')), paths)
  await rows[2].children[0].change(true)
  assert.equal(rows[0].children[0].checked, false)
  await selector.submit()
  assert.deepEqual(await chosen, [2, 3])
})

test('production import form defaults to current folder, multi-selects child and preserves fields', async () => {
  const h = harness()
  const pending = h.nameDialog('导入文件', null, '/Book.txt', { importMode: true, controller: h.controller })
  await new Promise(resolve => setImmediate(resolve))
  const form = h.dialogs[0]
  const inputs = form.content.querySelectorAll('input')
  inputs[7].value = 'keep note'; inputs[8].value = 'keep tag'
  const choose = form.content.querySelectorAll('button').find(button => button.textContent === '选择文件夹')
  const choosing = choose.click(); await new Promise(resolve => setImmediate(resolve))
  const selector = h.dialogs[1]
  assert.equal(selector.options.nested, true)
  const boxes = selector.content.querySelectorAll('input')
  assert.deepEqual(boxes.map(box => box.checked), [false, true, false])
  await boxes[2].change(true)
  assert.equal(boxes[0].checked, false)
  await selector.submit(); await choosing
  assert.match(form.panel.querySelector('.primary').textContent, /2 个文件夹/)
  await form.submit()
  assert.equal(form.closed, true)
  assert.deepEqual((await pending).importResult, success)
  assert.deepEqual(h.sent[0].payload.folderIds, [2, 3])
  assert.equal(h.sent[0].payload.note, 'keep note')
  assert.deepEqual(h.sent[0].payload.tags, ['keep tag'])
})

test('cancelled chooser restores original targets; invalid name keeps form and target choice', async () => {
  const h = harness()
  void h.nameDialog('导入文件', null, '/Book.txt', { importMode: true, controller: h.controller })
  await new Promise(resolve => setImmediate(resolve))
  const form = h.dialogs[0]
  const choosing = form.content.querySelectorAll('button').find(button => button.textContent === '选择文件夹').click()
  await new Promise(resolve => setImmediate(resolve))
  const selector = h.dialogs[1]; await selector.content.querySelectorAll('input')[2].change(true)
  selector.options.buttons[0].action(selector.close); await choosing
  assert.match(form.panel.querySelector('.primary').textContent, /1 个文件夹/)
  const subtitle = form.content.querySelectorAll('input')[3]; subtitle.value = 'One - Two'
  await form.submit()
  assert.equal(form.closed, false)
  assert.equal(h.focused(), subtitle)
  assert.equal(h.sent.length, 0)
})

test('deleted target remains visible and blocks submission until replaced', async () => {
  const h = harness()
  void h.nameDialog('导入文件', null, '/Book.txt', { importMode: true, controller: h.controller })
  await new Promise(resolve => setImmediate(resolve))
  h.setTree([{ id: 1, name: 'Library', children: [] }])
  const form = h.dialogs[0]; await form.submit()
  assert.equal(form.closed, false)
  assert.equal(h.sent.length, 0)
  const text = node => [node.textContent || '', ...(node.children || []).map(text)].join(' ')
  assert.match(text(form.content), /已删除的文件夹/)
  assert.match(text(form.content), /目标文件夹已失效/)
})

test('known failure keeps editable form; unknown outcome freezes draft and guards dismissal', async () => {
  for (const fail of ['disk_full', 'backend']) {
    const h = harness({ fail })
    void h.nameDialog('导入文件', null, '/Book.txt', { importMode: true, controller: h.controller })
    await new Promise(resolve => setImmediate(resolve))
    const form = h.dialogs[0]; const note = form.content.querySelectorAll('input')[7]; note.value = 'retain'
    await form.submit()
    assert.equal(form.closed, false)
    assert.equal(note.value, 'retain')
    assert.equal(note.disabled, fail === 'backend')
    assert.equal(form.options.canDismiss(), fail !== 'backend')
    if (fail === 'backend') {
      assert.match(form.panel.querySelector('.primary').textContent, /确认结果/)
      form.close(); assert.equal(form.closed, false)
      await form.options.buttons[0].action(form.close)
      assert.equal(h.askCount(), 1)
      assert.equal(form.closed, true)
      assert.equal(h.saved.size, 1)
    }
  }
})
