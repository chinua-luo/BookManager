// Pure helpers for the folder-import progress dialog. The dialog polls the
// backend every PROGRESS_POLL_INTERVAL_MS and must stop as soon as the job is
// no longer running, so all state decisions stay here and are unit tested.
export const PROGRESS_POLL_INTERVAL_MS = 250
const STATES = new Set(['running', 'done', 'cancelled', 'failed', 'unknown'])

const count = value => Math.max(0, Math.trunc(Number(value) || 0))

export function normalizeProgress(value) {
  const source = value && typeof value === 'object' ? value : {}
  return {
    jobId: typeof source.jobId === 'string' ? source.jobId : '',
    state: STATES.has(source.state) ? source.state : 'unknown',
    examined: count(source.examined),
    imported: count(source.imported),
    totalFiles: count(source.totalFiles),
    folderCount: count(source.folderCount),
    errorCount: count(source.errorCount),
    message: typeof source.message === 'string' ? source.message : ''
  }
}

export function progressPercent(value) {
  const progress = normalizeProgress(value)
  if (!progress.totalFiles) return null
  return Math.max(0, Math.min(100, Math.round((progress.examined / progress.totalFiles) * 100)))
}

export function shouldStopPolling(value) {
  return normalizeProgress(value).state !== 'running'
}

export function progressText(value) {
  const progress = normalizeProgress(value)
  const counts = `已检查 ${progress.examined} / 共 ${progress.totalFiles} 个文件，已导入 ${progress.imported} 个`
  if (progress.state === 'done') return `导入完成：${counts}`
  if (progress.state === 'cancelled') return `导入已取消：${counts}`
  if (progress.state === 'failed') return `导入失败：${counts}${progress.errorCount ? `，失败 ${progress.errorCount} 个` : ''}`
  return `正在导入：${counts}`
}

export function isImportCancelled(notification) {
  return notification?.errorType === 'import_cancelled'
}
