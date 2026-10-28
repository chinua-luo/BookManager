import assert from 'node:assert/strict'
import test from 'node:test'
import { submitNameForm } from '../src/name-validation.js'
import { assertAllowedBridgeRequest } from '../ipc-policy.mjs'
import { businessError, notificationFromError } from '../user-errors.mjs'

function formHarness() {
  let values = { nameParts: { mainTitle: 'Book', subtitle: 'One - Two', edition: '1' }, note: 'keep my note', tags: ['tag'] }
  const accepted = []; const focused = []; const controls = {}; const errors = {}
  for (const key of Object.keys(values.nameParts)) {
    const attributes = new Map()
    controls[key] = { attributes, removeAttribute: key => attributes.delete(key), setAttribute: (key, value) => attributes.set(key, value), focus: () => focused.push(key) }
    errors[key] = { textContent: '' }
  }
  return { controls, errors, focused, accepted, read: () => structuredClone(values), edit: subtitle => { values.nameParts.subtitle = subtitle }, accept: values => accepted.push(values) }
}

test('invalid subtitle focuses the field and keeps all values until corrected', async () => {
  const h = formHarness()
  const message = "副标题名不能包含分隔符 ' - '，请删除两侧空格或改用其他标点。"
  assert.equal(await submitNameForm({ ...h, validate: async () => ({ valid: false, field: 'subtitle', message }) }), false)
  assert.deepEqual(h.accepted, [])
  assert.deepEqual(h.focused, ['subtitle'])
  assert.equal(h.errors.subtitle.textContent, message)
  assert.equal(h.controls.subtitle.attributes.get('aria-invalid'), 'true')
  assert.equal(h.read().nameParts.subtitle, 'One - Two')
  h.edit('One-Two')
  assert.equal(await submitNameForm({ ...h, validate: async parts => { assert.equal(parts.subtitle, 'One-Two'); return { valid: true } } }), true)
  assert.equal(h.accepted.length, 1)
  assert.deepEqual(h.accepted[0], { nameParts: { mainTitle: 'Book', subtitle: 'One-Two', edition: '1' }, note: 'keep my note', tags: ['tag'] })
  assert.equal(h.errors.subtitle.textContent, '')
  assert.equal(h.controls.subtitle.attributes.has('aria-invalid'), false)
})

test('connection failure leaves the form and filled values available for retry', async () => {
  const h = formHarness()
  await assert.rejects(submitNameForm({ ...h, validate: async () => { throw new Error('connection failed') } }), /connection failed/)
  assert.deepEqual(h.accepted, [])
  assert.equal(h.read().note, 'keep my note')
})

test('editing while validation is pending requires validation of the new values', async () => {
  const h = formHarness()
  let finish
  const pending = submitNameForm({ ...h, validate: () => new Promise(resolve => { finish = resolve }) })
  h.edit('changed - again')
  finish({ valid: true })
  assert.equal(await pending, false)
  assert.deepEqual(h.accepted, [])
})

test('specific naming reason survives Electron IPC error decoration', () => {
  const msg = "副标题名不能包含分隔符 ' - '，请删除两侧空格或改用其他标点。"
  const error = businessError({ error_type: 'invalid_name', msg, level: 'error', data: null }, '/api/import/file')
  const notice = notificationFromError(new Error(`Error invoking remote method: Error: ${error.message}`))
  assert.equal(notice.title, '名称填写有误')
  assert.equal(notice.msg, msg)
  assert.equal(notice.errorType, 'invalid_name')
})

// Exercise the real dialog adapter so failed validation cannot dismiss the form.
import { readFile } from 'node:fs/promises'
const renderer = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')

test('production name dialog stays open with inputs intact and accepts a corrected name', async () => {
  let focused; let form; let options; let closed = 0
  class Node {
    children = []; attributes = new Map(); listeners = new Map(); value = ''
    append(...nodes) { this.children.push(...nodes); if (this.tag === 'select' && !this.value) this.value = nodes[0]?.text || '' }
    setAttribute(key, value) { this.attributes.set(key, value) }
    removeAttribute(key) { this.attributes.delete(key) }
    addEventListener(key, value) { this.listeners.set(key, value) }
    focus() { focused = this }
  }
  const document = { createElement: tag => Object.assign(new Node(), { tag }) }
  const element = (tag, className, textContent) => Object.assign(document.createElement(tag), { className, textContent })
  const calls = []
  const window = { bookManager: { post: async (path, { nameParts }) => {
    assertAllowedBridgeRequest('POST', path, { nameParts })
    calls.push({ path, nameParts })
    return nameParts.subtitle.includes(' - ') ? { valid: false, field: 'subtitle', message: '副标题名包含保留分隔符，请修改。' } : { valid: true }
  } } }
  const modal = (_title, content, config) => { form = content; options = config; return { panel: {} } }
  const start = renderer.indexOf('async function nameDialog(')
  const end = renderer.indexOf('\nasync function showItem(', start)
  const create = new Function('window', 'document', 'Option', 'modal', 'element', 'submitNameForm', 'nameDisplay', `return (${renderer.slice(start, end)})`)
  const nameDialog = create(window, document, class { constructor(text) { this.text = text } }, modal, element, submitNameForm, () => 'Book')
  const result = nameDialog('重命名文件', { nameParts: { mainTitle: 'Book', subtitle: 'One - Two' }, note: 'keep note', tags: ['tag'] })
  const inputs = form.children.filter(node => node.tag === 'label' && node.children[0]?.tag === 'input').map(node => node.children[0])
  const subtitle = inputs[3]
  const submit = options.buttons.find(button => button.primary).action
  const close = () => { closed++; options.onDismiss() }
  await submit(close)
  assert.equal(closed, 0)
  assert.equal(focused, subtitle)
  assert.equal(subtitle.value, 'One - Two')
  assert.equal(inputs[7].value, 'keep note')
  assert.equal(subtitle.attributes.get('aria-invalid'), 'true')
  subtitle.value = 'One-Two'
  subtitle.listeners.get('input')()
  assert.equal(subtitle.attributes.has('aria-invalid'), false)
  await submit(close)
  const accepted = await result
  assert.equal(closed, 1)
  assert.equal(accepted.nameParts.subtitle, 'One-Two')
  assert.equal(accepted.note, 'keep note')
  assert.deepEqual(accepted.tags, ['tag'])
  assert.deepEqual(calls.map(call => call.path), ['/api/name-validate', '/api/name-validate'])
})
