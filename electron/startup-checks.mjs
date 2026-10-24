import { constants } from 'node:fs'
import { readFile, stat, access } from 'node:fs/promises'
import path from 'node:path'

const failures = {
  dependency_missing: ['依赖缺失', '核心 Python 依赖无法加载，请修复 Python 环境后重试'],
  data_permission: ['权限不足', '数据目录无写入权限，请检查文件夹权限'],
  disk_full: ['磁盘空间不足', '剩余空间不足，无法完成本次操作'],
  data_corrupt: ['数据目录损坏', '书库数据库完整性检查失败，请恢复有效备份后重试'],
  data_unavailable: ['数据目录不可用', '无法访问书库数据目录，请检查目录位置和存储设备'],
  frontend_missing: ['界面文件缺失', '界面构建文件缺失或损坏，请重新构建或安装软件'],
  python_missing: ['依赖缺失', '未能启动 Python，请检查 Python 是否安装及启动路径配置'],
  startup_failed: ['程序启动失败', '无法启动后端服务，请检查 Python 环境、数据目录权限和剩余磁盘空间后重试']
}

export function startupFailure(type = 'startup_failed') {
  const [title, msg] = failures[type] || failures.startup_failed
  return new Error(`BM_NOTICE:${JSON.stringify({ title, msg, errorType: type in failures ? type : 'startup_failed', level: 'fatal' })}`)
}

export async function checkFrontend(directory) {
  try {
    const index = await readFile(path.join(directory, 'index.html'), 'utf8')
    const references = [...index.matchAll(/<(?:script|link)\b[^>]*(?:src|href)=["']([^"']+\.(?:js|css)(?:\?[^"']*)?)["']/gi)].map(match => match[1])
    if (!references.some(reference => /\.js(?:\?|$)/.test(reference))) throw new Error('Missing JavaScript entry')
    for (const reference of [...references, 'vendor/djvu/djvu.js', 'vendor/djvu/djvu_viewer.js']) {
      if (/^[a-z]+:|^\/\//i.test(reference)) throw new Error('Unexpected external startup asset')
      const filename = path.resolve(directory, decodeURIComponent(reference.replace(/^\//, '').split('?')[0]))
      if (!filename.startsWith(path.resolve(directory) + path.sep)) throw new Error('Invalid startup asset path')
      await access(filename, constants.R_OK)
      const asset = await stat(filename)
      if (!asset.isFile() || asset.size === 0) throw new Error('Startup asset is empty or is not a file')
    }
  } catch (error) {
    console.error('Frontend preflight failed:', error)
    throw startupFailure('frontend_missing')
  }
}
