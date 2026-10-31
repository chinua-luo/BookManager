// Pure pagination helpers for incremental folder-content loading.
// This module deliberately has no DOM / Electron dependencies so it can be
// exercised directly by node:test (see tests/pagination.test.mjs).

export const PAGE_SIZE = 200
export const MAX_PAGE_LIMIT = 1000
// Distance (in px) from the bottom of the content list that triggers the next page.
export const LOAD_MORE_THRESHOLD_PX = 300

// An item id is the only identity we need to avoid appending a row twice.
export function paginationItemKey(item) {
  const id = item?.id
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

export function createPaginationState(folderId = null) {
  return {
    folderId,
    // Token of the owning folder request; appended pages are dropped once the
    // user switches folders even if the id happens to be selected again later.
    folderToken: null,
    offset: 0,
    total: 0,
    loaded: 0,
    loading: false,
    hasMore: false,
    items: [],
    ids: new Set()
  }
}

// The renderer keeps these values; the main process uses the same helper to
// validate IPC arguments before building the bridge path.
export function normalizeFolderPageParams(folderId, offset = 0, limit = PAGE_SIZE) {
  if (!Number.isSafeInteger(folderId) || folderId <= 0) throw new TypeError('folderId 必须是正整数')
  if (!Number.isSafeInteger(offset) || offset < 0) throw new TypeError('offset 必须是非负整数')
  if (!Number.isSafeInteger(limit) || limit < 1) throw new TypeError('limit 必须是正整数')
  return { folderId, offset, limit: Math.min(limit, MAX_PAGE_LIMIT) }
}

// True when the list is close enough to the bottom and another page exists.
export function shouldLoadMore(metrics = {}) {
  const {
    scrollTop = 0,
    clientHeight = 0,
    scrollHeight = 0,
    hasMore = false,
    loading = false,
    threshold = LOAD_MORE_THRESHOLD_PX
  } = metrics
  if (!hasMore || loading) return false
  const remaining = scrollHeight - scrollTop - clientHeight
  return remaining <= threshold
}

// Offset to request for the next page; null means "do not request now".
export function nextPageOffset(state) {
  if (!state || state.loading || !state.hasMore) return null
  if (!Number.isSafeInteger(state.offset) || state.offset < 0) return null
  return state.offset
}

// Merge one API page into the pagination state. Items already appended (same
// id) are skipped, so a duplicated or overlapping page never renders twice.
export function mergePage(state, page) {
  const base = state || createPaginationState()
  const pageItems = Array.isArray(page?.items) ? page.items : []
  const meta = page?.pagination && typeof page.pagination === 'object' ? page.pagination : {}
  const ids = new Set(base.ids)
  const items = [...base.items]
  for (const item of pageItems) {
    const key = paginationItemKey(item)
    if (key !== null) {
      if (ids.has(key)) continue
      ids.add(key)
    }
    items.push(item)
  }
  const offset = Number.isSafeInteger(meta.offset) ? meta.offset + pageItems.length : base.offset + pageItems.length
  const total = Number.isSafeInteger(meta.total) ? Math.max(0, meta.total) : Math.max(base.total, items.length)
  let hasMore = typeof meta.hasMore === 'boolean' ? meta.hasMore : offset < total
  // A page that made no progress (empty response, or already past the total)
  // must stop the auto-loader even if the server flags more.
  if (pageItems.length === 0 || offset >= total) hasMore = false
  return { ...base, items, ids, offset, total, loaded: items.length, loading: false, hasMore }
}

// contentCount text, e.g. "已加载 200 / 5000 项"; keeps the old "0 项" for empty folders.
export function paginationCountText(state) {
  const total = Number.isSafeInteger(state?.total) && state.total > 0 ? state.total : 0
  if (total === 0) return '0 项'
  const loaded = Math.max(0, Math.min(total, Number.isSafeInteger(state?.loaded) ? state.loaded : 0))
  return `已加载 ${loaded} / ${total} 项`
}
