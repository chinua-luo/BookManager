import assert from 'node:assert/strict'
import test from 'node:test'

import {
  isImportCancelled,
  normalizeProgress,
  PROGRESS_POLL_INTERVAL_MS,
  progressPercent,
  progressText,
  shouldStopPolling
} from '../src/import-progress.js'

test('progress polling interval matches the backend contract', () => {
  assert.equal(PROGRESS_POLL_INTERVAL_MS, 250)
})

test('normalizeProgress keeps only positive integer counters and known states', () => {
  assert.deepEqual(normalizeProgress({ jobId: 'job-1', state: 'running', examined: '3', imported: 1.9, totalFiles: -2, folderCount: null, errorCount: Number.NaN, message: 'ok' }), {
    jobId: 'job-1', state: 'running', examined: 3, imported: 1, totalFiles: 0, folderCount: 0, errorCount: 0, message: 'ok'
  })
  assert.equal(normalizeProgress(null).state, 'unknown')
  assert.equal(normalizeProgress({ state: 'mystery' }).state, 'unknown')
  assert.equal(normalizeProgress({ jobId: 5 }).jobId, '')
})

test('progress text always reports examined, total and imported counts', () => {
  assert.equal(progressText({ state: 'running', examined: 2, totalFiles: 5, imported: 1 }), '正在导入：已检查 2 / 共 5 个文件，已导入 1 个')
  assert.equal(progressText({ state: 'done', examined: 5, totalFiles: 5, imported: 5 }), '导入完成：已检查 5 / 共 5 个文件，已导入 5 个')
  assert.equal(progressText({ state: 'cancelled', examined: 1, totalFiles: 5, imported: 0 }), '导入已取消：已检查 1 / 共 5 个文件，已导入 0 个')
  assert.equal(progressText({ state: 'failed', examined: 2, totalFiles: 5, imported: 1, errorCount: 2 }), '导入失败：已检查 2 / 共 5 个文件，已导入 1 个，失败 2 个')
  assert.match(progressText({}), /^正在导入：已检查 0 \/ 共 0 个文件，已导入 0 个$/)
})

test('progress percent is clamped and indeterminate without a total', () => {
  assert.equal(progressPercent({ examined: 1, totalFiles: 4 }), 25)
  assert.equal(progressPercent({ examined: 9, totalFiles: 4 }), 100)
  assert.equal(progressPercent({ examined: 0, totalFiles: 0 }), null)
  assert.equal(progressPercent(undefined), null)
})

test('polling stops for every terminal state but continues while running', () => {
  for (const state of ['done', 'cancelled', 'failed', 'unknown']) assert.equal(shouldStopPolling({ state }), true, state)
  assert.equal(shouldStopPolling({ state: 'running' }), false)
  assert.equal(shouldStopPolling(null), true)
})

test('cancellation detection uses the curated business error type', () => {
  assert.equal(isImportCancelled({ errorType: 'import_cancelled' }), true)
  assert.equal(isImportCancelled({ errorType: 'import_failed' }), false)
  assert.equal(isImportCancelled(null), false)
})
