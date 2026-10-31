import assert from 'node:assert/strict'
import test from 'node:test'
import { createImportController, importSummaryText } from '../src/import-controller.js'
import { folderTargets } from '../src/folder-targets.js'
import { businessError, notificationFromError } from '../user-errors.mjs'

function storage() {
  const values = new Map()
  return { values, getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }
}
const payload = { path: '/Book.pdf', nameParts: { mainTitle: 'Book' }, folderIds: [1, 2], note: 'note', tags: ['tag'] }
const result = { requestId: 'request-1', documentId: 9, entries: [{ itemId: 1, folderId: 1 }, { itemId: 2, folderId: 2 }], automatic: [], stats: { targetCount: 2, created: 2, existing: 0, automatic: 0 } }
const issue = (type, msg = 'failure') => businessError({ error_type: type, msg, level: 'error' })

test('request and draft are persisted before import; success clears the draft', async () => {
  const state = storage(); const sent = []
  const controller = createImportController({ storage: state, libraryId: 'library', newId: () => 'request-1', api: { post: async (path, body) => {
    assert.equal(state.values.size, 1)
    assert.equal(controller.lastRequestId, 'request-1')
    sent.push({ path, body }); return result
  } } })
  assert.deepEqual(await controller.run(payload), result)
  assert.deepEqual(sent, [{ path: '/api/import/file', body: { ...payload, requestId: 'request-1', libraryId: 'library' } }])
  assert.equal(controller.pending, null)
  assert.equal(controller.lastRequestId, null)
  assert.equal(state.values.size, 0)
})

test('confirming a recovered success also clears the support request identifier', async () => {
  const state = storage()
  state.setItem('bookmanager.pendingImport.library', JSON.stringify({ requestId: 'request-1', payload }))
  const controller = createImportController({ storage: state, libraryId: 'library', api: { get: async () => ({ state: 'success', result }), post: async () => { throw new Error('must not repost') } } })
  assert.equal(controller.lastRequestId, 'request-1')
  assert.deepEqual(await controller.run(payload), result)
  assert.equal(controller.lastRequestId, null)
})

test('server success with lost response is recovered without creating another import', async () => {
  let posts = 0
  const controller = createImportController({ storage: storage(), libraryId: 'library', api: { post: async () => { posts++; throw issue('backend') }, get: async () => ({ state: 'success', result }) } })
  assert.deepEqual(await controller.run(payload), result)
  assert.equal(posts, 1)
  assert.equal(controller.pending, null)
})

test('unknown outcome locks the original draft across restart and returns the confirmed receipt', async () => {
  const state = storage(); let posts = 0
  const controller = createImportController({ storage: state, libraryId: 'library', newId: () => 'request-1', api: { post: async () => { posts++; throw issue('backend') }, get: async () => ({ state: 'pending' }) } })
  await assert.rejects(controller.run(payload), error => notificationFromError(error).errorType === 'import_unknown')
  assert.deepEqual(controller.pending, { requestId: 'request-1', payload })
  assert.equal(controller.lastRequestId, 'request-1')
  await assert.rejects(controller.run({ ...payload, folderIds: [3] }), error => notificationFromError(error).errorType === 'request_conflict')
  const recovered = createImportController({ storage: state, libraryId: 'library', api: { get: async path => { assert.equal(path, '/api/import/requests/request-1'); return { state: 'success', result } }, post: async () => { throw new Error('must not repost') } } })
  assert.deepEqual(await recovered.run(payload), result)
  assert.equal(recovered.pending, null)
  assert.equal(posts, 1)
})

test('not-found status retries the original immutable request ID, not a new request', async () => {
  const state = storage()
  state.setItem('bookmanager.pendingImport.library', JSON.stringify({ requestId: 'request-1', payload }))
  const controller = createImportController({ storage: state, libraryId: 'library', newId: () => { throw new Error('must reuse ID') }, api: { get: async () => ({ state: 'not_found' }), post: async (_path, body) => { assert.equal(body.requestId, 'request-1'); return result } } })
  assert.deepEqual(await controller.run(payload), result)
})

test('confirmed failures allow editing and assign a new ID on the next submission', async () => {
  const ids = []; let number = 0
  const controller = createImportController({ storage: storage(), libraryId: 'library', newId: () => `request-${++number}`, api: { post: async (_path, body) => { ids.push(body.requestId); if (number === 1) throw issue('target_missing', 'target removed'); return result } } })
  await assert.rejects(controller.run(payload), /target removed/)
  assert.equal(controller.pending, null)
  await controller.run({ ...payload, folderIds: [1] })
  assert.deepEqual(ids, ['request-1', 'request-2'])
})

test('confirmed rollback after a lost response clears pending state with the specific reason', async () => {
  const controller = createImportController({ storage: storage(), libraryId: 'library', api: { post: async () => { throw issue('backend') }, get: async () => ({ state: 'failed', errorType: 'disk_full', message: 'disk full' }) } })
  await assert.rejects(controller.run(payload), error => notificationFromError(error).errorType === 'disk_full')
  assert.equal(controller.pending, null)
})

test('lookup failure preserves pending state; storage write failure sends no request', async () => {
  let posts = 0
  const controller = createImportController({ storage: storage(), libraryId: 'library', api: { post: async () => { posts++; throw issue('backend') }, get: async () => { throw issue('backend') } } })
  await assert.rejects(controller.run(payload), /import_unknown/)
  assert.ok(controller.pending)
  const broken = createImportController({ storage: { ...storage(), setItem: () => { throw new Error('quota exceeded') } }, libraryId: 'library', api: { post: async () => { posts++ } } })
  await assert.rejects(broken.run(payload), /尚未开始导入/)
  assert.equal(broken.pending, null)
  assert.equal(posts, 1)
})

test('drafts are scoped to the library and returned copies cannot mutate pending requests', async () => {
  const state = storage()
  const controller = createImportController({ storage: state, libraryId: 'one', api: { post: async () => { throw issue('backend') }, get: async () => ({ state: 'pending' }) } })
  await assert.rejects(controller.run(payload))
  const copy = controller.pending; copy.payload.folderIds.push(5)
  assert.deepEqual(controller.pending.payload.folderIds, [1, 2])
  assert.equal(createImportController({ storage: state, libraryId: 'two', api: {} }).pending, null)
})

test('target paths distinguish duplicate names, parent/child, repeated and deleted targets', () => {
  const tree = [{ id: 1, name: 'Library', children: [{ id: 2, name: 'Same', children: [{ id: 3, name: 'Child' }] }, { id: 4, name: 'Other', children: [{ id: 5, name: 'Same' }] }] }]
  assert.deepEqual(folderTargets(tree, [2, 3, 5, 2, 99]), [
    { id: 2, path: 'Library / Same', missing: false }, { id: 3, path: 'Library / Same / Child', missing: false },
    { id: 5, path: 'Library / Other / Same', missing: false }, { id: 99, path: '已删除的文件夹（编号：99）', missing: true }
  ])
})

test('summaries distinguish new and existing mirrors and automatic article filing', () => {
  assert.match(importSummaryText(result), /新建 2 个镜像/)
  assert.match(importSummaryText({ ...result, stats: { ...result.stats, created: 0, existing: 2 } }), /均已有该文档/)
  assert.match(importSummaryText({ ...result, automatic: [{ folderId: 3 }] }), /Articles/)
})


test('generic response failure after commit uses the receipt instead of declaring failure', async () => {
  const controller = createImportController({ storage: storage(), libraryId: 'library', api: { post: async () => { throw issue('import_failed') }, get: async () => ({ state: 'success', result }) } })
  assert.deepEqual(await controller.run(payload), result)
})

test('corrupted pending state blocks only import and keeps the record for recovery', async () => {
  const state = storage(); state.setItem('bookmanager.pendingImport.library', 'broken JSON')
  const controller = createImportController({ storage: state, libraryId: 'library', api: { post: async () => { throw new Error('must not send') } } })
  assert.ok(controller.restoreIssue)
  await assert.rejects(controller.run(payload), /暂停导入功能/)
  assert.equal(state.values.size, 1)
})
