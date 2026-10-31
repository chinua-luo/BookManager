import assert from 'node:assert/strict'
import test from 'node:test'
import {
  LOAD_MORE_THRESHOLD_PX,
  MAX_PAGE_LIMIT,
  PAGE_SIZE,
  createPaginationState,
  mergePage,
  nextPageOffset,
  normalizeFolderPageParams,
  paginationCountText,
  shouldLoadMore
} from '../src/pagination.js'

const metrics = (overrides = {}) => ({
  scrollTop: 1000,
  clientHeight: 700,
  scrollHeight: 2000,
  hasMore: true,
  loading: false,
  ...overrides
})

test('page size and load threshold constants match the backend contract', () => {
  assert.equal(PAGE_SIZE, 200)
  assert.equal(MAX_PAGE_LIMIT, 1000)
  assert.equal(LOAD_MORE_THRESHOLD_PX, 300)
})

test('folder page IPC arguments are validated and clamped', () => {
  assert.deepEqual(normalizeFolderPageParams(5), { folderId: 5, offset: 0, limit: PAGE_SIZE })
  assert.deepEqual(normalizeFolderPageParams(5, 200, 200), { folderId: 5, offset: 200, limit: 200 })
  assert.deepEqual(normalizeFolderPageParams(5, 0, MAX_PAGE_LIMIT), { folderId: 5, offset: 0, limit: 1000 })
  // limit is clamped instead of rejected so a stale renderer cannot ask for an unbounded page.
  assert.deepEqual(normalizeFolderPageParams(5, 0, 5000), { folderId: 5, offset: 0, limit: MAX_PAGE_LIMIT })
  for (const folderId of [0, -1, 1.5, '7', null, undefined, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => normalizeFolderPageParams(folderId), TypeError)
  }
  for (const offset of [-1, 1.5, '0', null, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => normalizeFolderPageParams(5, offset), TypeError)
  }
  for (const limit of [0, -5, 1.5, '200', null, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => normalizeFolderPageParams(5, 0, limit), TypeError)
  }
})

test('shouldLoadMore only fires near the bottom while more pages exist', () => {
  assert.equal(shouldLoadMore(metrics()), true) // remaining exactly 300
  assert.equal(shouldLoadMore(metrics({ scrollTop: 999 })), false) // remaining 301
  assert.equal(shouldLoadMore(metrics({ scrollHeight: 2001 })), false)
  assert.equal(shouldLoadMore(metrics({ hasMore: false })), false)
  assert.equal(shouldLoadMore(metrics({ loading: true })), false)
  assert.equal(shouldLoadMore(metrics({ scrollTop: 0, clientHeight: 400, scrollHeight: 400 })), true)
  assert.equal(shouldLoadMore(metrics({ threshold: 0 })), false)
  assert.equal(shouldLoadMore(metrics({ scrollTop: 1300, threshold: 0 })), true)
  assert.equal(shouldLoadMore(), false)
})

test('nextPageOffset asks only when a page is available and not already loading', () => {
  assert.equal(nextPageOffset(createPaginationState(1)), null)
  const ready = { ...createPaginationState(1), hasMore: true, offset: 0 }
  assert.equal(nextPageOffset(ready), 0)
  assert.equal(nextPageOffset({ ...ready, offset: 200 }), 200)
  assert.equal(nextPageOffset({ ...ready, loading: true }), null)
  assert.equal(nextPageOffset({ ...ready, hasMore: false }), null)
  assert.equal(nextPageOffset({ ...ready, offset: -1 }), null)
  assert.equal(nextPageOffset(null), null)
})

test('mergePage appends a page, tracks totals and never duplicates rows', () => {
  const initial = createPaginationState(7)
  const first = mergePage(initial, {
    items: [{ id: 1 }, { id: 2 }],
    pagination: { offset: 0, limit: 200, total: 5, hasMore: true }
  })
  assert.equal(first.folderId, 7)
  assert.deepEqual(first.items.map(item => item.id), [1, 2])
  assert.equal(first.loaded, 2)
  assert.equal(first.offset, 2)
  assert.equal(first.total, 5)
  assert.equal(first.hasMore, true)
  assert.equal(first.loading, false)

  // Overlapping page (a duplicated row) must not be rendered twice, yet the
  // offset still advances by what the server says it returned.
  const second = mergePage(first, {
    items: [{ id: 2 }, { id: 3 }],
    pagination: { offset: 2, limit: 200, total: 5, hasMore: true }
  })
  assert.deepEqual(second.items.map(item => item.id), [1, 2, 3])
  assert.equal(second.loaded, 3)
  assert.equal(second.offset, 4)
  assert.equal(second.hasMore, true)

  // The merged state is a new object; the previous one is left untouched.
  assert.equal(initial.loaded, 0)
  assert.equal(initial.items.length, 0)
  assert.equal(first.items.length, 2)
})

test('mergePage stops pagination when the total is reached or the page is empty', () => {
  const ready = { ...createPaginationState(3), hasMore: true, offset: 0 }
  const lastPage = mergePage(ready, {
    items: [{ id: 1 }, { id: 2 }],
    pagination: { offset: 0, limit: 200, total: 2, hasMore: true }
  })
  assert.equal(lastPage.hasMore, false)

  const empty = mergePage(ready, {
    items: [],
    pagination: { offset: 0, limit: 200, total: 10, hasMore: true }
  })
  assert.equal(empty.hasMore, false)
  assert.equal(empty.loaded, 0)

  const exhausted = mergePage({ ...ready, offset: 4, total: 4 }, {
    items: [{ id: 9 }],
    pagination: { offset: 4, limit: 200, total: 4, hasMore: true }
  })
  assert.equal(exhausted.hasMore, false)
})

test('mergePage tolerates a backend without the pagination block', () => {
  const merged = mergePage(createPaginationState(1), { items: [{ id: 1 }, { id: 2 }] })
  assert.equal(merged.loaded, 2)
  assert.equal(merged.total, 2)
  assert.equal(merged.offset, 2)
  assert.equal(merged.hasMore, false)
  assert.equal(nextPageOffset(merged), null)
})

test('createPaginationState resets state when the folder changes', () => {
  const loaded = mergePage(createPaginationState(1), {
    items: [{ id: 1 }, { id: 2 }],
    pagination: { offset: 0, limit: 200, total: 9, hasMore: true }
  })
  assert.equal(loaded.loaded, 2)
  assert.equal(loaded.hasMore, true)
  const switched = createPaginationState(2)
  assert.equal(switched.folderId, 2)
  assert.equal(switched.offset, 0)
  assert.equal(switched.total, 0)
  assert.equal(switched.loaded, 0)
  assert.equal(switched.loading, false)
  assert.equal(switched.hasMore, false)
  assert.deepEqual(switched.items, [])
  assert.equal(switched.ids.size, 0)
})

test('content count shows load progress and keeps 0 项 for empty folders', () => {
  assert.equal(paginationCountText(createPaginationState(1)), '0 项')
  assert.equal(paginationCountText({ total: 0, loaded: 0 }), '0 项')
  assert.equal(paginationCountText({ total: 5000, loaded: 200 }), '已加载 200 / 5000 项')
  assert.equal(paginationCountText({ total: 5000, loaded: 0 }), '已加载 0 / 5000 项')
  assert.equal(paginationCountText({ total: 5000, loaded: 6000 }), '已加载 5000 / 5000 项')
  assert.equal(paginationCountText({ total: 5000, loaded: Number.NaN }), '已加载 0 / 5000 项')
})
