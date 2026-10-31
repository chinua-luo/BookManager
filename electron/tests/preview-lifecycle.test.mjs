import assert from 'node:assert/strict'
import test from 'node:test'

import { createPreviewResourceManager, waitForLayout } from '../src/preview-lifecycle.js'

test('layout waiting resolves once the host is measurable', async () => {
  let time = 0
  const frames = []
  let width = 0
  const ready = waitForLayout({
    isCurrent: () => true,
    measure: () => ({ width, height: width ? 400 : 0 }),
    schedule: callback => { frames.push(callback); return frames.length },
    now: () => time,
    timeoutMs: 5000
  })
  assert.equal(width, 0)
  width = 300
  time = 16
  frames[0]()
  assert.equal(await ready, true)
})

test('layout waiting stops after the timeout instead of looping forever', async () => {
  let time = 0
  const frames = []
  const result = waitForLayout({
    measure: () => ({ width: 0, height: 0 }),
    schedule: callback => { frames.push(callback); return frames.length },
    now: () => time,
    timeoutMs: 50
  })
  time = 20; frames[0]()
  time = 40; frames[1]()
  time = 60; frames[2]()
  assert.equal(await result, false)
  assert.equal(frames.length, 3)
})

test('layout waiting abandons a superseded preview request', async () => {
  let current = true
  const frames = []
  const ready = waitForLayout({
    isCurrent: () => current,
    measure: () => ({ width: 0, height: 0 }),
    schedule: callback => { frames.push(callback); return frames.length },
    timeoutMs: 5000
  })
  current = false
  frames[0]()
  assert.equal(await ready, false)
  assert.equal(frames.length, 1)
})

test('switching away from the viewer unloads the old document and aborts text fetches', async () => {
  const events = []
  const controllers = []
  const manager = createPreviewResourceManager({
    unloadViewer: reason => events.push(`unload:${reason}`),
    abortText: controller => events.push(`abort:${controller.id}`),
    createAbortController: () => { const controller = { id: controllers.length + 1, abort() {} }; controllers.push(controller); return controller }
  })
  manager.begin('viewer')
  manager.release()
  await manager.settle()
  assert.deepEqual(events, ['unload:source-changed'])
  assert.equal(manager.kind, null)

  const first = manager.beginText()
  manager.beginText()
  assert.deepEqual(events, ['unload:source-changed', 'abort:1'])
  manager.settleText(controllers[1])
  manager.release()
  await manager.settle()
  assert.deepEqual(events, ['unload:source-changed', 'abort:1'])
  assert.equal(first.id, 1)
})

test('switching between two viewer documents releases the previous one', async () => {
  const events = []
  const manager = createPreviewResourceManager({ unloadViewer: reason => events.push(reason) })
  manager.begin('viewer')
  manager.begin('viewer')
  await manager.settle()
  assert.deepEqual(events, ['source-changed'])
  manager.release()
  await manager.settle()
  assert.deepEqual(events, ['source-changed', 'source-changed'])
})
