// Pure view-model helpers for rendering search results in chunks. Search still
// returns at most 500 entries; rendering them all at once creates hundreds of
// DOM rows even though only the first screen is visible.

export const SEARCH_RESULT_CHUNK_SIZE = 100

// Files are grouped by document id (all mirrors of one document share a row
// group); mirrors/tags render one row per entry. `units` are the render units
// the chunker counts, not raw entries.
export function searchResultUnits(kind, results) {
  const entries = Array.isArray(results) ? results : []
  if (kind !== 'files') return entries.map(entry => ({ kind: 'entry', entries: [entry] }))
  const groups = new Map()
  for (const entry of entries) {
    const key = String(entry?.item?.documentId)
    const existing = groups.get(key)
    if (existing) existing.entries.push(entry)
    else groups.set(key, { kind: 'group', entries: [entry] })
  }
  return [...groups.values()].map(unit => (unit.entries.length === 1 ? { kind: 'entry', entries: unit.entries } : unit))
}

// Slice the next chunk; `rendered` is the number of units already in the DOM.
export function nextSearchChunk(units, rendered = 0, size = SEARCH_RESULT_CHUNK_SIZE) {
  const all = Array.isArray(units) ? units : []
  const chunkSize = Number.isSafeInteger(size) && size > 0 ? size : SEARCH_RESULT_CHUNK_SIZE
  const start = Number.isSafeInteger(rendered) && rendered > 0 ? Math.min(rendered, all.length) : 0
  const end = Math.min(all.length, start + chunkSize)
  return {
    units: all.slice(start, end),
    rendered: end,
    remaining: all.length - end,
    hasMore: end < all.length
  }
}

export function searchMoreLabel(remaining) {
  const count = Number.isSafeInteger(remaining) && remaining > 0 ? remaining : 0
  return `显示更多（剩余 ${count} 项）`
}
