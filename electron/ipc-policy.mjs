const GET_PATHS = new Set(['/api/tree', '/api/settings', '/api/tags', '/api/shortcuts'])
const POST_PATHS = new Set([
  '/api/folders',
  '/api/import/file',
  '/api/import/folder',
  '/api/name-candidate',
  '/api/name-parse',
  '/api/name-metadata',
  '/api/search',
  '/api/crawl',
  '/api/settings/cache',
  '/api/settings/cache/clear',
  '/api/open-cache/sync',
  '/api/settings/data/migrate',
  '/api/settings/shortcuts',
  '/api/logs/events'
])
const GET_PATTERNS = [/^\/api\/folders\/\d+\/contents$/]
const POST_PATTERNS = [
  /^\/api\/folders\/\d+\/(?:rename|move|delete)$/,
  /^\/api\/items\/\d+\/(?:rename|move|mirror|delete|delete-underlying)$/,
  /^\/api\/documents\/\d+\/replace$/
]

export function allowedBridgePath(method, pathname) {
  if (typeof pathname !== 'string' || pathname.includes('?') || pathname.includes('#')) return false
  const paths = method === 'GET' ? GET_PATHS : POST_PATHS
  const patterns = method === 'GET' ? GET_PATTERNS : POST_PATTERNS
  return paths.has(pathname) || patterns.some(pattern => pattern.test(pathname))
}

export function assertAllowedBridgeRequest(method, pathname, payload = undefined) {
  if (!allowedBridgePath(method, pathname)) throw new TypeError('Bridge API path is not allowed')
  if (payload === undefined) return
  const encoded = JSON.stringify(payload)
  if (encoded === undefined || encoded.length > 1024 * 1024) throw new TypeError('Bridge API payload is too large')
}
