import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

const renderer = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')
const html = await readFile(new URL('../src/index.html', import.meta.url), 'utf8')
const styles = await readFile(new URL('../src/style.css', import.meta.url), 'utf8')
const main = await readFile(new URL('../main.mjs', import.meta.url), 'utf8')
const importController = await readFile(new URL('../src/import-controller.js', import.meta.url), 'utf8')

function section(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker)
  const end = start < 0 ? -1 : source.indexOf(endMarker, start)
  assert.ok(start >= 0 && end > start, `missing section ${startMarker} -> ${endMarker}`)
  return source.slice(start, end)
}

test('folder and refresh requests are sequenced so outdated responses are dropped', () => {
  assert.match(renderer, /const folderRequests = createRequestSequence\(\)/)
  assert.match(renderer, /const refreshRequests = createRequestSequence\(\)/)
  const select = section(renderer, 'async function selectFolder(', 'function renderTree(')
  assert.match(select, /const request = folderRequests\.begin\(\)/)
  assert.match(select, /if \(!folderRequests\.isCurrent\(request\)\) return false/)
  const refresh = section(renderer, 'async function refresh(', '\nasync function openDefault(')
  assert.match(refresh, /const request = refreshRequests\.begin\(\)/)
  assert.match(refresh, /if \(!refreshRequests\.isCurrent\(request\)\) return false/)
})

test('row selection drives both the shared item state and the toolbar', () => {
  assert.match(renderer, /const rowItems = new WeakMap\(\)/)
  assert.match(renderer, /const rowFolders = new WeakMap\(\)/)
  assert.match(renderer, /function setSelectedItem\(item\)/)
  assert.match(renderer, /nextRowIndex\(rows\.length, rows\.indexOf\(document\.activeElement\), event\.key\)/)
  const mark = section(renderer, 'function markContentSelection(', 'function setSelectedItem(')
  assert.match(mark, /setSelectedItem\(rowItems\.get\(button\) \|\| null\)/)
  const toolbar = section(renderer, 'function updateToolbarState()', 'function markTreeSelection(')
  assert.match(toolbar, /const available = Boolean\(selectedItem\)/)
  assert.match(toolbar, /button\.disabled = !available/)
  assert.match(toolbar, /openMenuSummary/)
  assert.match(html, /id="open-menu-summary"/)
})

test('the folder tree remembers expansion and highlights the current folder', () => {
  assert.match(renderer, /const expandedFolderIds = new Set\(\)/)
  const tree = section(renderer, 'function renderTree(', 'async function refresh(')
  assert.match(tree, /details\.open = expandedFolderIds\.has\(folder\.id\)/)
  assert.match(tree, /details\.addEventListener\('toggle'/)
  assert.match(tree, /aria-current/)
  assert.match(renderer, /expandedFolderIds\.add\(folder\.id\)/)
  assert.match(renderer, /expandedFolderIds\.delete\(folder\.id\)/)
  assert.match(renderer, /function markTreeSelection\(\)/)
  assert.match(renderer, /function expandAncestors\(id\)/)
})

test('modal keyboard handling lives on the document and is removed when the dialog closes', () => {
  const modal = section(renderer, 'function modal(', 'function ask(')
  assert.match(modal, /document\.addEventListener\('keydown', onKeyDown, true\)/)
  assert.match(modal, /document\.removeEventListener\('keydown', onKeyDown, true\)/)
  assert.doesNotMatch(modal, /panel\.addEventListener\('keydown'/)
  assert.match(modal, /dialogKeyAction/)
  assert.match(modal, /focusTrapTarget/)
  const risk = section(renderer, 'function confirmRecursiveRisk(', '\nfunction crawl(')
  assert.doesNotMatch(risk, /document\.addEventListener\('keydown'/)
})

test('shortcut capture never stores Escape, Tab, modifiers or auto-repeat', () => {
  const dialog = section(renderer, 'async function shortcutDialog(', '\nasync function contextAction(')
  assert.match(dialog, /const accelerator = acceleratorFromKeyboardEvent\(event\)/)
  assert.match(dialog, /if \(accelerator === null\) return/)
  assert.doesNotMatch(dialog, /event\.key === 'Backspace'/)
})

test('preview switching releases old sources, aborts text fetches and shows a matching error card', () => {
  assert.match(renderer, /createPreviewResourceManager/)
  assert.match(renderer, /getApi\?\.\(\)/)
  assert.match(renderer, /\.unload\(reason\)/)
  const coordinator = section(renderer, 'const previewCoordinator = createPreviewCoordinator', 'function folderSummary(')
  assert.match(coordinator, /beginText\(\)/)
  assert.match(coordinator, /signal: controller\.signal/)
  assert.match(coordinator, /waitForViewerLayout\(isCurrent\)/)
  assert.match(coordinator, /if \(!layoutReady\) throw new Error/)
  assert.match(coordinator, /await previewResources\.settle\(\)/)
  assert.match(renderer, /timeoutMs: 5000/)
  const errorCard = section(renderer, 'function previewError(', 'function fileIconClass(')
  assert.match(errorCard, /preview-error/)
  assert.match(errorCard, /用系统默认程序打开/)
})

test('accessibility hooks are present for status, focus, disabled and reduced motion', () => {
  assert.match(html, /id="status" role="status" aria-live="polite"/)
  assert.doesNotMatch(styles, /#folder-tree button:focus,/)
  assert.match(styles, /:focus-visible/)
  assert.match(styles, /prefers-reduced-motion: reduce/)
  assert.match(styles, /\.toolbar-button:disabled/)
  assert.match(styles, /#app-menu summary\.disabled/)
  assert.match(styles, /\.preview-error/)
})

test('context menus can be opened with the keyboard and closed with Escape', () => {
  assert.match(renderer, /isContextMenuKey\(event\)/)
  assert.match(renderer, /function openContextForRow\(row\)/)
  assert.match(renderer, /if \(openContextForRow\(focusedRow\(contentList\)\)\) event\.preventDefault\(\)/)
  assert.match(renderer, /if \(openContextForRow\(focusedRow\(folderTree\)\)\) event\.preventDefault\(\)/)
  assert.match(renderer, /contextReturnFocus/)
  assert.match(renderer, /event\.key !== 'Escape' \|\| contextElement\.hidden/)
  assert.match(renderer, /setAttribute\('role', 'menuitem'\)/)
})

test('cache settings build DOM nodes instead of interpolating settings into HTML', () => {
  const cache = section(renderer, 'async function cacheSettings(', 'function help(')
  assert.doesNotMatch(cache, /innerHTML/)
  const search = section(renderer, 'function openSearch()', 'function confirmRecursiveRisk(')
  assert.match(search, /content\.innerHTML = `<p class="dialog-hint">仅填写/)
})

test('successful imports clear the request identifier and location success updates the status bar', () => {
  assert.match(importController, /function complete\(result\) \{ clear\(\); lastRequestId = null; return result \}/)
  const location = section(main, 'async function showItemLocation(', 'function showContextMenu(')
  assert.match(location, /return true/)
  assert.match(location, /return false/)
  assert.match(renderer, /async function showLocation\(/)
  assert.match(renderer, /已在文件管理器中显示底层文件/)
})

test('truncated search results are explained in the status bar', () => {
  assert.match(renderer, /result\.truncated \? `搜索完成：仅显示前/)
  assert.match(renderer, /结果过多，请细化搜索条件/)
})
