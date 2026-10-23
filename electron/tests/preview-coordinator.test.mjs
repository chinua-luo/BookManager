import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createPreviewCoordinator } from '../src/preview-coordinator.js'

test('the currently displayed document is not loaded again', async () => {
  const loaded = []
  const coordinator = createPreviewCoordinator({ keyOf: item => item.id, load: async item => loaded.push(item.id) })

  assert.equal(await coordinator.show({ id: 1 }), true)
  assert.equal(await coordinator.show({ id: 1 }), false)
  assert.deepEqual(loaded, [1])
})

test('only the newest asynchronous preview can become active', async () => {
  const resolvers = new Map()
  const coordinator = createPreviewCoordinator({
    keyOf: item => item.id,
    load: item => new Promise(resolve => resolvers.set(item.id, resolve))
  })

  const first = coordinator.show({ id: 1 })
  const second = coordinator.show({ id: 2 })
  resolvers.get(2)()
  assert.equal(await second, true)
  resolvers.get(1)()
  assert.equal(await first, false)

  assert.equal(await coordinator.show({ id: 2 }), false)
})

test('invalidating a preview prevents an unfinished load from becoming active', async () => {
  let finish
  const coordinator = createPreviewCoordinator({
    keyOf: item => item.id,
    load: () => new Promise(resolve => { finish = resolve })
  })

  const pending = coordinator.show({ id: 1 })
  coordinator.invalidate()
  finish()
  assert.equal(await pending, false)
})

test('reselecting the active document cancels a newer pending preview', async () => {
  const resolvers = new Map()
  const coordinator = createPreviewCoordinator({
    keyOf: item => item.id,
    load: item => item.id === 1 ? Promise.resolve() : new Promise(resolve => resolvers.set(item.id, resolve))
  })

  await coordinator.show({ id: 1 })
  const second = coordinator.show({ id: 2 })
  assert.equal(await coordinator.show({ id: 1 }), false)
  resolvers.get(2)()
  assert.equal(await second, false)
  assert.equal(await coordinator.show({ id: 1 }), false)
})

test('errors from stale previews are ignored', async () => {
  let rejectFirst
  const coordinator = createPreviewCoordinator({
    keyOf: item => item.id,
    load: item => item.id === 1
      ? new Promise((_resolve, reject) => { rejectFirst = reject })
      : Promise.resolve()
  })

  const first = coordinator.show({ id: 1 })
  await coordinator.show({ id: 2 })
  rejectFirst(new Error('stale failure'))
  assert.equal(await first, false)
})
