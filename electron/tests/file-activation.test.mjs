import assert from 'node:assert/strict'
import { test } from 'node:test'

import { bindFileActivation, DEFAULT_CLICK_DELAY_MS } from '../src/file-activation.js'

class FakeElement {
  constructor() { this.listeners = new Map() }
  addEventListener(name, callback) { this.listeners.set(name, callback) }
  removeEventListener(name) { this.listeners.delete(name) }
  dispatch(name, event = {}) { this.listeners.get(name)?.({ detail: 0, preventDefault() {}, ...event }) }
}

function fakeTimers() {
  let nextId = 1
  const callbacks = new Map()
  return {
    setTimeout(callback, delay) { const id = nextId++; callbacks.set(id, { callback, delay }); return id },
    clearTimeout(id) { callbacks.delete(id) },
    flush() { for (const [id, timer] of [...callbacks]) { callbacks.delete(id); timer.callback() } },
    get pending() { return callbacks.size },
    get delays() { return [...callbacks.values()].map(timer => timer.delay) }
  }
}

test('a single pointer click previews after the double-click window', async () => {
  const element = new FakeElement()
  const timers = fakeTimers()
  const calls = []
  bindFileActivation(element, {
    select: () => calls.push('select'),
    preview: () => calls.push('preview'),
    open: () => calls.push('open')
  }, timers)

  element.dispatch('click', { detail: 1 })
  assert.deepEqual(calls, ['select'])
  assert.equal(timers.pending, 1)
  timers.flush()
  await Promise.resolve()
  assert.deepEqual(calls, ['select', 'preview'])
})

test('the fallback interval leaves room for the operating system double-click window', () => {
  assert.ok(DEFAULT_CLICK_DELAY_MS >= 500)
})

test('a configured system interval is used instead of the old fixed delay', () => {
  const element = new FakeElement()
  const timers = fakeTimers()
  bindFileActivation(element, { preview: () => {}, open: () => {}, delayMs: 650 }, timers)

  element.dispatch('click', { detail: 1 })

  assert.deepEqual(timers.delays, [650])
})

test('double click cancels preview and only opens the file', async () => {
  const element = new FakeElement()
  const timers = fakeTimers()
  const calls = []
  bindFileActivation(element, {
    select: () => calls.push('select'),
    preview: () => calls.push('preview'),
    open: () => calls.push('open')
  }, timers)

  element.dispatch('click', { detail: 1 })
  element.dispatch('click', { detail: 2 })
  element.dispatch('dblclick', { detail: 2 })
  timers.flush()
  await Promise.resolve()

  assert.deepEqual(calls, ['select', 'select', 'open'])
  assert.equal(timers.pending, 0)
})

test('keyboard activation previews immediately', async () => {
  const element = new FakeElement()
  const timers = fakeTimers()
  const calls = []
  bindFileActivation(element, {
    preview: () => calls.push('preview'),
    open: () => calls.push('open')
  }, timers)

  element.dispatch('click', { detail: 0 })
  await Promise.resolve()

  assert.deepEqual(calls, ['preview'])
  assert.equal(timers.pending, 0)
})

test('callback failures are reported without leaving a pending timer', async () => {
  const element = new FakeElement()
  const timers = fakeTimers()
  const failures = []
  bindFileActivation(element, {
    preview: async () => { throw new Error('preview failed') },
    open: () => {},
    onError: error => failures.push(error.message)
  }, timers)

  element.dispatch('click', { detail: 1 })
  timers.flush()
  await Promise.resolve()
  await Promise.resolve()

  assert.deepEqual(failures, ['preview failed'])
  assert.equal(timers.pending, 0)
})
