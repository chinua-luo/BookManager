import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

const renderer = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')
const ipcPolicy = await readFile(new URL('../ipc-policy.mjs', import.meta.url), 'utf8')

function section(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker)
  const end = start < 0 ? -1 : source.indexOf(endMarker, start)
  assert.ok(start >= 0 && end > start, `missing section ${startMarker} -> ${endMarker}`)
  return source.slice(start, end)
}

test('folder imports open a cancellable progress dialog with a job id', () => {
  const importFolder = section(renderer, 'async function importFolder()', 'async function renameItem(')
  assert.match(importFolder, /const jobId = crypto\.randomUUID\(\)/)
  assert.match(importFolder, /progressText\(progress\)/)
  assert.match(importFolder, /normalizeProgress\(value\)/)
  assert.match(importFolder, /shouldStopPolling\(progress\)/)
  assert.match(importFolder, /import-progress-text/)
  assert.match(importFolder, /canDismiss: \(\) => !running/)
  assert.match(importFolder, /label: '取消导入', danger: true/)
  assert.match(importFolder, /PROGRESS_POLL_INTERVAL_MS/)
  assert.match(importFolder, /\/api\/import\/folder\/progress\/\$\{jobId\}/)
  assert.match(importFolder, /\/api\/import\/folder\/cancel\/\$\{jobId\}/)
  assert.match(importFolder, /jobId \}\)/)
})

test('the import request and polling stop cleanly on success, cancellation and error', () => {
  const importFolder = section(renderer, 'async function importFolder()', 'async function renameItem(')
  assert.match(importFolder, /stopPolling\(\)/)
  assert.match(importFolder, /clearInterval\(pollTimer\)/)
  assert.match(importFolder, /finally \{ stopPolling\(\) \}/)
  assert.match(importFolder, /running = false; stopPolling\(\); dialog\.close\(\)/)
  assert.match(importFolder, /info\.errorType === 'import_cancelled'/)
  assert.match(importFolder, /已取消导入，本次未保留任何文件/)
  assert.match(importFolder, /notice\(\{ title: '导入已取消'/)
  assert.match(importFolder, /await refresh\(result\.folderId\)/)
  assert.match(importFolder, /已添加文件夹，导入/)
  assert.match(importFolder, /throw issue/)
})

test('the folder tree has exactly one delegated listener per event kind', () => {
  const tree = section(renderer, 'function renderTree(', 'async function refresh(')
  assert.doesNotMatch(tree, /addEventListener/)
  for (const [event, handler, capture] of [['click', 'handleTreeClick', ''], ['contextmenu', 'handleTreeContextMenu', ''], ['toggle', 'handleTreeToggle', ', true']]) {
    const pattern = new RegExp(`folderTree\\.addEventListener\\('${event}', ${handler}${capture.replace('(', '\\(')}`, 'g')
    assert.equal((renderer.match(pattern) || []).length, 1, `${event} delegation count`)
  }
  assert.doesNotMatch(tree, /summary\.addEventListener\('click'/)
  assert.doesNotMatch(tree, /details\.addEventListener\('toggle'/)
})

test('delegated tree handlers preserve select, context menu and expansion behavior', () => {
  assert.match(renderer, /function handleTreeClick\(event\)/)
  assert.match(renderer, /event\.target\.closest\?\.\('button\[data-folder-id\]'\)/)
  assert.match(renderer, /event\.target === summary/)
  assert.match(renderer, /selectFolder\(id\)\.catch\(error\)/)
  assert.match(renderer, /function handleTreeContextMenu\(event\)/)
  assert.match(renderer, /const folder = rowFolders\.get\(button\)/)
  assert.match(renderer, /openContext\(event, \{ kind: 'folder', folder \}\)/)
  assert.match(renderer, /function handleTreeToggle\(event\)/)
  assert.match(renderer, /if \(details\.open\) expandedFolderIds\.add\(id\); else expandedFolderIds\.delete\(id\)/)
  assert.match(renderer, /rowFolders\.set\(button, folder\)/)
})

test('ipc policy allowlists progress reads and cancel posts with the job id shape', () => {
  assert.match(ipcPolicy, /\/api\\\/import\\\/folder\\\/progress\\\/\[A-Za-z0-9_-\]\{1,80\}\$/)
  assert.match(ipcPolicy, /\/api\\\/import\\\/folder\\\/cancel\\\/\[A-Za-z0-9_-\]\{1,80\}\$/)
})
