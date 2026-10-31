import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

const renderer = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')
const preload = await readFile(new URL('../preload.cjs', import.meta.url), 'utf8')
const main = await readFile(new URL('../main.mjs', import.meta.url), 'utf8')

function section(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker)
  const end = start < 0 ? -1 : source.indexOf(endMarker, start)
  assert.ok(start >= 0 && end > start, `missing section ${startMarker} -> ${endMarker}`)
  return source.slice(start, end)
}

test('preload exposes a paged folder bridge next to the legacy one', () => {
  assert.match(preload, /folder: \(folderId\) => ipcRenderer\.invoke\('bridge:folder', folderId\)/)
  assert.match(preload, /folderPage: \(folderId, offset, limit\) => ipcRenderer\.invoke\('bridge:folder-page', folderId, offset, limit\)/)
})

test('main process validates page arguments and builds the query string itself', () => {
  assert.match(main, /import \{ normalizeFolderPageParams \} from '\.\/src\/pagination\.js'/)
  const handler = section(main, "ipcMain.handle('bridge:folder-page'", "ipcMain.handle('bridge:document-url'")
  assert.match(handler, /normalizeFolderPageParams\(folderId, offset, limit\)/)
  assert.match(handler, /readJson\(`\/api\/folders\/\$\{page\.folderId\}\/contents\?offset=\$\{page\.offset\}&limit=\$\{page\.limit\}`\)/)
})

test('renderer imports the pagination helpers and requests only the first page', () => {
  assert.match(renderer, /from '\.\/pagination\.js'/)
  for (const helper of ['createPaginationState', 'LOAD_MORE_THRESHOLD_PX', 'mergePage', 'nextPageOffset', 'PAGE_SIZE', 'paginationCountText', 'shouldLoadMore']) {
    assert.match(renderer, new RegExp(`\\b${helper}\\b`), `missing helper ${helper}`)
  }
  const select = section(renderer, 'async function selectFolder(', 'function renderTree(')
  assert.match(select, /const request = folderRequests\.begin\(\)/)
  assert.match(select, /pageRequests\.invalidate\(\)/)
  assert.match(select, /pagination = \{ \.\.\.createPaginationState\(id\), folderToken: request \}/)
  assert.match(select, /folderPage\(id, 0, PAGE_SIZE\)/)
  assert.match(select, /if \(!folderRequests\.isCurrent\(request\)\) return false/)
  assert.match(select, /appendPage\(payload\)/)
  // The old "fetch everything and render it in one go" path is gone.
  assert.doesNotMatch(select, /window\.bookManager\.folder\(/)
  assert.doesNotMatch(select, /sortedItems\(payload\.items, id\)\.forEach/)
  assert.doesNotMatch(select, /contentCount\.textContent/)
})

test('each page is merged and appended without duplicating rows', () => {
  const append = section(renderer, 'function appendPage(payload)', 'async function loadMoreItems()')
  assert.match(append, /mergePage\(pagination, \{ items: sortedItems\(payload\.items \|\| \[\], folderId\), pagination: payload\.pagination \}\)/)
  assert.match(append, /pagination\.items\.slice\(previous\)\.forEach\(item => contentList\.append\(fileRow\(item\)\)\)/)
  assert.match(append, /updateContentCount\(\)/)
  assert.match(renderer, /contentCount\.textContent = paginationCountText\(pagination\)/)
})

test('next pages are sequenced against both folder switches and in-flight appends', () => {
  const load = section(renderer, 'async function loadMoreItems(', 'function scheduleLoadMoreCheck(')
  assert.match(load, /const offset = nextPageOffset\(state\)/)
  assert.match(load, /if \(offset === null\) return false/)
  assert.match(load, /const request = pageRequests\.begin\(\)/)
  assert.match(load, /window\.bookManager\.folderPage\(state\.folderId, offset, PAGE_SIZE\)/)
  assert.match(load, /pageRequests\.isCurrent\(request\)/)
  assert.match(load, /pagination\.folderId !== state\.folderId/)
  assert.match(load, /folderRequests\.isCurrent\(state\.folderToken\)/)
})

test('scrolling near the bottom schedules one rAF-throttled page request', () => {
  assert.match(renderer, /contentList\.addEventListener\('scroll', scheduleLoadMoreCheck, \{ passive: true \}\)/)
  const schedule = section(renderer, 'function scheduleLoadMoreCheck(', 'async function selectFolder(')
  assert.match(schedule, /if \(scrollLoadFrame\) return/)
  assert.match(schedule, /scrollLoadFrame = requestAnimationFrame\(/)
  assert.match(schedule, /shouldLoadMore\(\{/)
  assert.match(schedule, /hasMore: pagination\.hasMore/)
  assert.match(schedule, /loading: pagination\.loading/)
  assert.match(schedule, /threshold: LOAD_MORE_THRESHOLD_PX/)
  assert.match(schedule, /if \(wantsMore\) loadMoreItems\(\)\.catch\(error\)/)
})

test('ArrowDown on the last row triggers the next page', () => {
  const keyboard = section(renderer, "contentList.addEventListener('keydown'", "folderTree.addEventListener('keydown'")
  assert.match(keyboard, /event\.key === 'ArrowDown' && pagination\.hasMore && !pagination\.loading/)
  assert.match(keyboard, /document\.activeElement === rows\[rows\.length - 1\]/)
  assert.match(keyboard, /loadMoreItems\(\)/)
  assert.match(keyboard, /rows\b/) // navigation still runs for all other rows
  assert.match(keyboard, /moveListSelection\(event, contentList, \(\) => selectedFolderId\)/)
})
