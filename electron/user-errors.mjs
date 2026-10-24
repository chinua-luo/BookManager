// Only curated business messages cross the Electron IPC error boundary.
export function userError(value, context = '') {
  const raw = String(value?.message || value || '')
  const rules = [
    [/内网|回环|private|loopback|SSRF|安全策略|不安全|公网/i, '安全拦截', '目标地址属于内网 / 回环地址或不安全地址，已被安全策略拦截', 'unsafe_address'],
    [/ENOSPC|No space left|磁盘空间/i, '磁盘空间不足', '剩余空间不足，无法完成本次操作', 'disk_full'],
    [/EACCES|EPERM|Permission denied|权限/i, '权限不足', '无法访问所需文件，请检查文件夹权限', 'permission_denied'],
    [/格式不支持|不支持.*格式|unsupported.*format/i, '导入失败', '该文件格式不支持，无法导入书库', 'unsupported_format'],
    [/请先选择文件/, '请选择文件', '请先选择需要操作的文件', 'selection_required'],
    [/无法读取|不存在|No such file|ENOENT/i, '文件不可用', '文件或文件夹不存在或无法读取，请检查后重试', 'file_unavailable'],
    [/网址|URL|https?:|系列编号/i, '网址无效', '请输入有效的公开网址或 Springer 系列编号', 'invalid_url']
  ]
  for (const [pattern, title, msg, errorType] of rules) if (pattern.test(raw)) return { title, msg, errorType, level: 'error' }
  const labels = context.includes('/import') ? ['导入失败', '文件导入失败，请检查文件是否可读取且内容完整']
    : context.includes('/search') ? ['搜索失败', '搜索执行失败，请检查搜索条件后重试']
    : context.includes('/crawl') ? ['爬取失败', '未能完成爬取，请检查网址、网络连接和站点访问限制']
    : context.includes('/settings/cache/clear') ? ['缓存清理失败', '未能清理缓存，请关闭正在使用缓存文件的程序后重试']
    : context.includes('/settings/data') ? ['迁移失败', '数据迁移失败，请检查目标文件夹为空且具有写入权限']
    : context.includes('/settings') ? ['设置保存失败', '未能保存设置，请检查输入内容后重试']
    : ['操作失败', '未能完成操作，请检查输入和文件状态后重试']
  return { title: labels[0], msg: labels[1], errorType: `operation_failed:${context || 'unknown'}`, level: 'error' }
}
export function notificationFromError(value) {
  // Electron decorates thrown errors; recover only our own curated fields.
  const raw = String(value?.message || value || '')
  const match = raw.match(/BM_NOTICE:(\{[^\n]+\})$/)
  if (match) {
    try {
      const parsed = JSON.parse(match[1])
      if (typeof parsed.msg === 'string' && typeof parsed.title === 'string' && typeof parsed.errorType === 'string') return parsed
    } catch { /* Fall back to a safe message. */ }
  }
  return userError(value)
}
export function businessError(value, context, fatal = false) {
  const notice = fatal ? { title: '程序异常', msg: '后端服务连接失败，请重启软件', errorType: 'backend', level: 'fatal' } : userError(value, context)
  return new Error(`BM_NOTICE:${JSON.stringify(notice)}`)
}
export function importSummary(result) {
  const failed = result.errors?.length || 0
  return failed ? { level: result.fileCount ? 'warning' : 'error', title: result.fileCount ? '部分导入失败' : '导入失败', errorType: 'import_partial', msg: `共 ${result.fileCount + failed} 个文件，成功导入 ${result.fileCount} 个，${failed} 个因无法读取或导入失败被跳过` } : null
}
export function crawlSummary(result) {
  const stats = result.stats || {}
  const messages = (result.messages || []).map(String)
  const skipped = (stats.skipped_cross_origin || 0) + (stats.skipped_safe_block || 0)
  const fallback = messages.some(msg => msg.includes('所有下载链接均失败'))
  const warnings = messages.some(msg => /^(跳过|安全拦截|起始网址发生重定向)/.test(msg))
  if (!skipped && !fallback && !warnings) return null
  return { title: fallback ? '未能下载文档' : '爬取结果提醒', level: 'warning', errorType: 'crawl_partial',
    msg: fallback ? '文档下载失败，已保存源页面供后续查看。请检查站点访问限制或更换网址。'
      : `已导入 ${result.downloaded} 个文件；部分链接因访问失败、安全限制或跨源限制已跳过。${skipped ? `其中跨源或安全拦截 ${skipped} 个。` : ''}` }
}
export function createFatalReporter(show, now = Date.now) {
  const recent = new Map()
  return (type, title, msg) => {
    const time = now()
    if (recent.has(type) && time - recent.get(type) < 10000) return false
    recent.set(type, time); show(title, msg); return true
  }
}
