import { businessError, notificationFromError } from '../user-errors.mjs'

const certainFailures = new Set(['invalid_targets', 'target_missing', 'invalid_request_id', 'invalid_name', 'file_unavailable', 'source_changed', 'library_busy', 'disk_full', 'permission_denied', 'unsupported_format', 'import_interrupted'])
const problem = (error_type, msg) => businessError({ error_type, msg, level: 'error' }, '/api/import/file')

export function importSummaryText(result) {
  const stats = result.stats
  const summary = stats.created ? `已完成：选择 ${stats.targetCount} 个文件夹，新建 ${stats.created} 个镜像，${stats.existing} 个位置已有镜像。`
    : `所选 ${stats.targetCount} 个文件夹中均已有该文档，无需重复创建。`
  const automatic = result.automatic || []
  const created = automatic.filter(entry => entry.created).length
  return summary + (automatic.length ? ` 已按 article 标签规则归档到 Articles（新建 ${created} 个，已有 ${automatic.length - created} 个）。` : '')
}

export function createImportController({ api, storage, libraryId, newId = () => crypto.randomUUID() }) {
  const key = `bookmanager.pendingImport.${libraryId}`
  let pending = null
  let restoreIssue = null
  let lastRequestId = null
  try {
    const stored = storage.getItem(key)
    if (stored) {
      const value = JSON.parse(stored)
      if (!value || !/^[A-Za-z0-9_-]{1,80}$/.test(value.requestId) || !value.payload || !Array.isArray(value.payload.folderIds) || typeof value.payload.path !== 'string' || !value.payload.nameParts) throw new Error('invalid stored request')
      pending = value; lastRequestId = value.requestId
    }
  } catch (error) {
    console.error('Cannot restore pending import:', error)
    restoreIssue = problem('import_state_unavailable', '无法读取待确认导入记录，为避免重复导入已暂停导入功能。请检查本地应用存储后重新打开程序。')
  }
  function clear() { storage.removeItem(key); pending = null }
  async function query() {
    const status = await api.get(`/api/import/requests/${pending.requestId}`)
    if (status.state === 'success') { const result = status.result; clear(); return result }
    if (status.state === 'failed') { clear(); throw problem(status.errorType, status.message) }
    return null
  }
  return {
    get libraryId() { return libraryId },
    get restoreIssue() { return restoreIssue },
    get lastRequestId() { return lastRequestId },
    get pending() { return pending ? structuredClone(pending) : null },
    async run(payload) {
      if (restoreIssue) throw restoreIssue
      if (pending && JSON.stringify(payload) !== JSON.stringify(pending.payload)) throw problem('request_conflict', '上次导入结果尚未确认，请先确认原请求结果。')
      if (pending) {
        const result = await query()
        if (result) return result
      } else {
        const candidate = { requestId: newId(), payload: structuredClone(payload) }
        try { storage.setItem(key, JSON.stringify(candidate)) }
        catch (error) { console.error('Cannot persist import request:', error); throw problem('import_state_unavailable', '无法保存导入请求记录，本次尚未开始导入，请检查本地应用存储后重试。') }
        pending = candidate; lastRequestId = candidate.requestId
      }
      try {
        const result = await api.post('/api/import/file', { ...pending.payload, requestId: pending.requestId, libraryId })
        clear()
        return result
      } catch (error) {
        if (notificationFromError(error).errorType === 'library_changed') throw error
        if (certainFailures.has(notificationFromError(error).errorType)) { clear(); throw error }
        try {
          const result = await query()
          if (result) return result
        } catch (statusError) {
          if (!pending) throw statusError
        }
        throw problem('import_unknown', `导入结果暂时无法确认，请保留此窗口，点击“确认结果 / 重试原请求”。请求标识：${pending.requestId}`)
      }
    }
  }
}
