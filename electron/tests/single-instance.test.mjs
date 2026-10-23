import assert from 'node:assert/strict'
import { test } from 'node:test'

import { acquireSingleInstance } from '../single-instance.mjs'

function fakeApp(lockGranted) {
  const listeners = new Map()
  return {
    quitCalls: 0,
    requestSingleInstanceLock() { return lockGranted },
    quit() { this.quitCalls += 1 },
    on(name, callback) { listeners.set(name, callback) },
    emit(name) { listeners.get(name)?.() },
    listeners
  }
}

test('secondary instances quit before starting another backend', () => {
  const app = fakeApp(false)

  assert.equal(acquireSingleInstance(app, () => null), false)
  assert.equal(app.quitCalls, 1)
  assert.equal(app.listeners.size, 0)
})

test('a second launch restores and focuses the existing window', () => {
  const calls = []
  const app = fakeApp(true)
  const window = {
    isDestroyed: () => false,
    isMinimized: () => true,
    restore: () => calls.push('restore'),
    show: () => calls.push('show'),
    focus: () => calls.push('focus')
  }

  assert.equal(acquireSingleInstance(app, () => window), true)
  app.emit('second-instance')

  assert.deepEqual(calls, ['restore', 'show', 'focus'])
})

test('a second launch is ignored while the primary window is unavailable', () => {
  const app = fakeApp(true)
  assert.equal(acquireSingleInstance(app, () => null), true)

  assert.doesNotThrow(() => app.emit('second-instance'))
})
