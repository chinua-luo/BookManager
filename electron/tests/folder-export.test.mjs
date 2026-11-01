import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

import {
  EXPORT_DIALOG_TITLE,
  exportDialogHint,
  exportDialogRows,
  exportSuccessMessage,
  exportTargetPath,
  formatExportBytes
} from '../src/folder-export.js'

const renderer = await readFile(new URL('../src/main.js', import.meta.url), 'utf8')

function section(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker)
  const end = start < 0 ? -1 : source.indexOf(endMarker, start)
  assert.ok(start >= 0 && end > start, `missing section ${startMarker} -> ${endMarker}`)
  return source.slice(start, end)
}

test('export sizes are formatted without leaking floating point noise', () => {
  assert.equal(formatExportBytes(0), '0 B')
  assert.equal(formatExportBytes(Number.NaN), '0 B')
  assert.equal(formatExportBytes(-2048), '0 B')
  assert.equal(formatExportBytes(512), '512 B')
  assert.equal(formatExportBytes(1024), '1.0 KB')
  assert.equal(formatExportBytes(1536), '1.5 KB')
  assert.equal(formatExportBytes(1024 ** 2 * 2.25), '2.3 MB')
  assert.equal(formatExportBytes(1024 ** 3), '1.0 GB')
  assert.equal(formatExportBytes(1024 ** 4 * 3), '3072.0 GB')
})

test('export target folders are joined for both POSIX and Windows parents', () => {
  assert.equal(exportTargetPath('/Users/snow/Desktop', '资料'), '/Users/snow/Desktop/资料')
  assert.equal(exportTargetPath('/Users/snow/Desktop/', '资料'), '/Users/snow/Desktop/资料')
  assert.equal(exportTargetPath('C:\\Users\\snow\\Desktop', '资料'), 'C:\\Users\\snow\\Desktop\\资料')
  assert.equal(exportTargetPath('C:\\Users\\snow\\Desktop\\', '资料'), 'C:\\Users\\snow\\Desktop\\资料')
  assert.equal(exportTargetPath('', '资料'), '资料')
  assert.equal(exportTargetPath('/Users/snow', '  '), '/Users/snow')
})

test('export confirmation copy names the source, destination and created folder', () => {
  assert.equal(EXPORT_DIALOG_TITLE, '导出文件夹')
  const hint = exportDialogHint('资料')
  assert.match(hint, /“资料”/)
  assert.match(hint, /同名目录不会被覆盖/)
  assert.match(hint, /导出期间请勿关闭程序/)
  assert.equal(exportDialogRows({
    folderPath: '学科 / 资料',
    targetPath: '/Users/snow/Desktop',
    folderName: '资料',
    itemCount: 12,
    folderCount: 4,
    uniqueSize: 2048
  }).map(row => row.label).join('|'), '源文件夹|导出位置|将创建目录|文件数|文件夹数|占用空间')
})

test('export rows show the created folder and human readable statistics', () => {
  const rows = exportDialogRows({
    folderPath: '学科 / 资料',
    targetPath: '/Users/snow/Desktop',
    folderName: '资料',
    itemCount: 12,
    folderCount: 4,
    uniqueSize: 2048
  })
  assert.deepEqual(rows.map(row => row.value), ['学科 / 资料', '/Users/snow/Desktop', '/Users/snow/Desktop/资料', '12 个', '4 个', '2.0 KB'])
  assert.equal(exportDialogRows({ folderName: '资料' })[3].value, '0 个')
  assert.equal(exportDialogRows({ folderName: '资料' })[5].value, '0 B')
})

test('export success copy reports file, folder and byte totals', () => {
  const msg = exportSuccessMessage({ folderName: '资料', targetPath: '/Users/snow/Desktop/', fileCount: 3, folderCount: 4, copiedBytes: 2048 })
  assert.match(msg, /已将“资料”导出到 \/Users\/snow\/Desktop\/资料/)
  assert.match(msg, /3 个文件/)
  assert.match(msg, /4 个文件夹/)
  assert.match(msg, /共 2\.0 KB。/)
})

test('folder context menu offers export between move and sorting', () => {
  const showContext = section(renderer, 'function showContext(', 'function openContext(')
  const folderMenu = showContext.slice(showContext.indexOf("if (target.kind === 'folder')"), showContext.indexOf('} else {'))
  const move = folderMenu.indexOf("contextButton('移动', 'folder-move')")
  const exportItem = folderMenu.indexOf("contextButton('导出文件夹…', 'folder-export')")
  const sort = folderMenu.indexOf("contextButton('排序方式', '')")
  assert.ok(move >= 0 && exportItem > move && sort > exportItem, 'export must sit between move and sort')
  assert.match(folderMenu, /sortMenu\.style\.top = '154px'/)
  assert.doesNotMatch(folderMenu, /sortMenu\.style\.top = '124px'/)
})

test('context menu dispatches the export action and help documents it', () => {
  assert.match(renderer, /if \(action === 'folder-export'\) return exportFolder\(folder\);/)
  const dispatch = section(renderer, 'async function contextAction(', 'async function action(')
  assert.ok(dispatch.indexOf("folder-move") < dispatch.indexOf("folder-export"))
  assert.ok(dispatch.indexOf("folder-export") < dispatch.indexOf("folder-delete"))
  const help = section(renderer, 'function help()', 'async function shortcutDialog(')
  assert.match(help, /导出文件夹：选择父目录后递归复制为同名文件夹/)
  assert.match(help, /文件夹可新建、重命名、移动、导出、排序、删除/)
})

test('export dialog reads stats, asks for a target and posts the documented request', () => {
  const exportFolder = section(renderer, 'async function exportFolder(', 'async function importFile(')
  assert.match(exportFolder, /window\.bookManager\.folderPage\(folder\.id, 0, 1\)/)
  assert.match(exportFolder, /const folderName = page\.folder\?\.name \|\| folder\.name/)
  assert.match(exportFolder, /const stats = page\.stats \|\| \{\}/)
  assert.match(exportFolder, /window\.bookManager\.selectFolder\('选择导出位置（将在其中创建同名文件夹）'\)/)
  assert.match(exportFolder, /if \(!targetPath\) return/)
  assert.match(exportFolder, /modal\(EXPORT_DIALOG_TITLE, content, \{ nested: true, buttons:/)
  assert.match(exportFolder, /window\.bookManager\.post\(`\/api\/folders\/\$\{folder\.id\}\/export`, \{ targetPath \}\)/)
  assert.match(exportFolder, /exportDialogHint\(folderName\)/)
  assert.match(exportFolder, /exportDialogRows\(\{ folderPath: sourcePath, targetPath, folderName, itemCount: stats\.itemCount, folderCount: stats\.folderCount, uniqueSize: stats\.uniqueSize \}\)/)
  assert.doesNotMatch(exportFolder, /\/api\/logs\/events/)
})

test('export confirmation blocks duplicate submits and reports one outcome', () => {
  const exportFolder = section(renderer, 'async function exportFolder(', 'async function importFile(')
  assert.match(exportFolder, /let submitting = false/)
  assert.match(exportFolder, /if \(submitting\) return/)
  assert.match(exportFolder, /buttons\.forEach\(button => \{ button\.disabled = true \}\)/)
  assert.match(exportFolder, /textContent = '正在导出…'/)
  assert.match(exportFolder, /notice\(\{ title: '导出完成', msg: exportSuccessMessage\(\{ \.\.\.result, folderName: result\.folderName \|\| folderName \}\), errorType: 'export_complete', level: 'success' \}\)/)
  const catches = exportFolder.match(/catch \(issue\) \{\n        close\(\)\n        error\(issue\)/g) || []
  assert.equal(catches.length, 1, 'a failed export reports exactly one error')
  assert.match(exportFolder, /finally \{\n        submitting = false\n        buttons\.forEach\(button => \{ button\.disabled = false \}\)/)
})
