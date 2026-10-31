// Pure helpers for bounded text previews. The bridge already understands HTTP
// Range requests for documents, so a large text file is fetched by bytes rather
// than read into the renderer in full. No DOM/Electron imports here so the
// helpers can be tested with node:test.

export const TEXT_PREVIEW_LIMIT_BYTES = 2 * 1024 * 1024

function wholeByteCount(value) {
  const number = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : 0
}

// Returns { range, init }: init is passed straight to fetch(). When the file
// fits under the limit (or its size is unknown) no Range header is added and
// the existing full-read behaviour is preserved.
export function textPreviewRequest(size, limit = TEXT_PREVIEW_LIMIT_BYTES) {
  if (!Number.isSafeInteger(limit) || limit < 1) throw new TypeError('text preview limit must be a positive integer')
  const total = wholeByteCount(size)
  if (total === 0 || total <= limit) return { range: null, init: {} }
  const end = limit - 1
  return {
    range: { start: 0, end, total },
    init: { headers: { Range: `bytes=0-${end}` } }
  }
}

export function formatPreviewBytes(bytes) {
  const value = wholeByteCount(bytes)
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(value >= 10 * 1024 * 1024 ? 0 : 1)} MB`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${value} B`
}

// Empty string when the full file is shown; otherwise a short status line for
// the preview footer.
export function textPreviewNotice(range) {
  if (!range) return ''
  return `文件较大（${formatPreviewBytes(range.total)}），仅显示前 ${formatPreviewBytes(range.end + 1)}。`
}
