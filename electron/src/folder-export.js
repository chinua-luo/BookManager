// Pure copy and formatting helpers for the folder export flow. They never touch
// the DOM or the bridge API so they can be unit tested in plain Node.
export const EXPORT_DIALOG_TITLE = '导出文件夹'

export function formatExportBytes(value) {
  const units = ['B', 'KB', 'MB', 'GB']
  const requested = Number(value)
  let number = Number.isFinite(requested) && requested > 0 ? requested : 0
  for (const unit of units) {
    if (number < 1024 || unit === 'GB') return unit === 'B' ? `${number} B` : `${number.toFixed(1)} ${unit}`
    number /= 1024
  }
}

function usesWindowsSeparator(path) {
  return path.includes('\\') || /^[A-Za-z]:/.test(path)
}

export function exportTargetPath(parentPath, folderName) {
  const parent = String(parentPath ?? '').trim()
  const name = String(folderName ?? '').trim()
  if (!parent) return name
  if (!name) return parent
  if (/[\\/]$/.test(parent)) return `${parent}${name}`
  return `${parent}${usesWindowsSeparator(parent) ? '\\' : '/'}${name}`
}

export function exportDialogHint(folderName) {
  return `将在所选位置创建“${folderName}”文件夹并复制其中全部内容；同名目录不会被覆盖，导出期间请勿关闭程序。`
}

export function exportDialogRows({ folderPath = '', targetPath = '', folderName = '', itemCount = 0, folderCount = 0, uniqueSize = 0 } = {}) {
  return [
    { label: '源文件夹', value: String(folderPath || folderName) },
    { label: '导出位置', value: String(targetPath) },
    { label: '将创建目录', value: exportTargetPath(targetPath, folderName) },
    { label: '文件数', value: `${Number(itemCount) || 0} 个` },
    { label: '文件夹数', value: `${Number(folderCount) || 0} 个` },
    { label: '占用空间', value: formatExportBytes(uniqueSize) }
  ]
}

export function exportSuccessMessage({ folderName = '', targetPath = '', fileCount = 0, folderCount = 0, copiedBytes = 0 } = {}) {
  return `已将“${folderName}”导出到 ${exportTargetPath(targetPath, folderName)}：${Number(fileCount) || 0} 个文件、${Number(folderCount) || 0} 个文件夹，共 ${formatExportBytes(copiedBytes)}。`
}
