import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, protocol, session, shell, systemPreferences } from 'electron'
import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { readDoubleClickDelayMs } from './double-click.mjs'
import { createDocumentProtocolHandler, DOCUMENT_SCHEME, documentUrl } from './document-protocol.mjs'
import { assertAllowedBridgeRequest } from './ipc-policy.mjs'
import { acquireSingleInstance } from './single-instance.mjs'
import { checkFrontend, startupFailure } from './startup-checks.mjs'
import { createMainLogger } from './diagnostics.mjs'
import { businessError, userError, notificationFromError, createFatalReporter } from './user-errors.mjs'

protocol.registerSchemesAsPrivileged([
  {
    scheme: DOCUMENT_SCHEME,
    privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true }
  }
])

const electronDir = path.dirname(fileURLToPath(import.meta.url))
const projectDir = process.env.BOOKMANAGER_PROJECT_DIR || path.resolve(electronDir, '..')
const staticDir = path.join(projectDir, 'electron', 'dist')
const appIcon = path.join(projectDir, 'book.png')
const token = randomBytes(32).toString('hex')
let bridge = null
let bridgeUrl = null
let mainWindow = null
let quitting = false
const reportFatal = createFatalReporter((title, msg) => dialog.showErrorBox(title, msg))
const primaryInstance = acquireSingleInstance(app, () => mainWindow)

function pythonCommand() {
  if (process.env.BOOKMANAGER_PYTHON) {
    return { executable: process.env.BOOKMANAGER_PYTHON, prefix: [] }
  }
  return process.platform === 'win32'
    ? { executable: 'py', prefix: ['-3'] }
    : { executable: 'python3', prefix: [] }
}

function bridgeFetch(pathname, options = {}) {
  if (!bridgeUrl) throw new Error('BookManager bridge is not ready')
  return fetch(`${bridgeUrl}${pathname}`, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, ...(options.headers || {}) }
  }).catch(error => {
    console.error('BookManager bridge connection:', error)
    if (!quitting) reportFatal('backend', '程序异常', '后端服务连接失败，请重启软件')
    throw businessError(error, pathname, true)
  })
}

function startBridge() {
  const command = pythonCommand()
  const args = [
    ...command.prefix,
    '-m', 'bookmanager.startup',
    '--port', '0',
    '--static-dir', staticDir
  ]
  if (process.env.BOOKMANAGER_DATA_HOME) {
    args.push('--data-home', process.env.BOOKMANAGER_DATA_HOME)
  }
  bridge = spawn(command.executable, args, {
    cwd: projectDir,
    env: { ...process.env, BOOKMANAGER_BRIDGE_TOKEN: token },
    stdio: ['ignore', 'pipe', 'pipe']
  })
  bridge.stderr.on('data', (chunk) => console.error(`BookManager bridge: ${chunk}`))
  return new Promise((resolve, reject) => {
    let output = ''
    const timer = setTimeout(() => reject(new Error('BookManager bridge did not start in time')), 30000)
    bridge.stdout.on('data', (chunk) => {
      output += chunk.toString()
      const lines = output.split('\n')
      output = lines.pop()
      for (const line of lines) {
        try {
          const event = JSON.parse(line)
          if (event.event === 'startup-error') {
            clearTimeout(timer)
            console.error('Backend preflight failed:', event.error_type, 'diagnostics:', event.log_path)
            reject(startupFailure(event.error_type))
          }
          if (event.event === 'ready') {
            clearTimeout(timer)
            bridgeUrl = `http://127.0.0.1:${event.port}`
            resolve()
          }
        } catch {
          // The bridge reserves stdout for one-line JSON lifecycle events.
        }
      }
    })
    bridge.once('error', (error) => {
      clearTimeout(timer)
      console.error('Python process could not start:', error)
      reject(error.code === 'ENOENT' ? startupFailure('python_missing') : error)
    })
    bridge.once('exit', (code) => {
      if (!bridgeUrl) {
        clearTimeout(timer)
        reject(new Error(`BookManager bridge exited before startup (${code ?? 'unknown'})`))
      } else if (!quitting) {
        console.error('Backend exited unexpectedly:', code)
        reportFatal('backend', '程序异常', '后端服务意外退出，请重启软件')
      }
    })
  })
}

async function responseJson(response, pathname) {
  const detail = await response.json().catch(error => { console.error('Invalid bridge response:', error); throw businessError(error, pathname) })
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) throw businessError('Invalid response envelope', pathname)
  if (!response.ok || detail.ok === false) {
    console.error('BookManager request failed:', pathname, response.status, detail)
    throw businessError('data' in detail ? detail : detail.msg || detail.error, pathname)
  }
  if (typeof detail.ok === 'boolean' && 'data' in detail) {
    if (detail.data === null || typeof detail.data !== 'object' || Array.isArray(detail.data)) throw businessError('Invalid response data', pathname)
    const data = { ...detail.data }
    if (detail.level === 'warning') data._notification = notificationFromError(businessError(detail, pathname))
    return data
  }
  return detail
}

async function readJson(pathname) {
  return responseJson(await bridgeFetch(pathname), pathname)
}

async function postJson(pathname, payload = {}) {
  return responseJson(await bridgeFetch(pathname, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }), pathname)
}

async function logOperation(payload) {
  try {
    await postJson('/api/logs/events', payload)
  } catch (error) {
    console.error(`BookManager operation log: ${error instanceof Error ? error.message : error}`)
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 980,
    minHeight: 620,
    title: 'BookManager',
    icon: appIcon,
    webPreferences: {
      preload: path.join(electronDir, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })
  mainWindow.setMenu(Menu.getApplicationMenu())
  mainWindow.setMenuBarVisibility(true)
  mainWindow.webContents.on('render-process-gone', (_event, details) => { console.error('Renderer process exited:', details); if (!quitting) reportFatal('renderer', '界面异常', '界面进程意外退出，请重启软件') })
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  mainWindow.webContents.on('will-navigate', (event, targetUrl) => {
    if (!bridgeUrl || new URL(targetUrl).origin !== new URL(bridgeUrl).origin) event.preventDefault()
  })
  mainWindow.loadURL(bridgeUrl)
}

function sendRendererAction(action) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('menu-action', action)
  }
}

async function openItem(itemId, fallbackDocumentName = '') {
  const started = performance.now()
  let documentName = fallbackDocumentName
  try {
    const result = await readJson(`/api/items/${encodeURIComponent(itemId)}/open`)
    documentName = result.documentName
    const error = await shell.openPath(result.path)
    if (error) throw new Error(error)
    await logOperation({ event: 'file.open.default', message: '使用系统默认程序打开文件', documentName, result: 'success', durationMs: performance.now() - started })
    return true
  } catch (error) {
    await logOperation({ event: 'file.open.default', message: '使用系统默认程序打开文件', documentName, result: 'failure', error: error instanceof Error ? error.message : String(error), durationMs: performance.now() - started })
    if (notificationFromError(error).level === 'fatal') return false
    mainWindow?.webContents.send('business-error', { ...userError(error), title: '打开文件失败', msg: '无法使用系统默认程序打开文件，请检查文件关联和文件是否可读取', errorType: 'open_failed' })
    return false
  }
}

async function showItemLocation(itemId, fallbackDocumentName = '') {
  const started = performance.now()
  let documentName = fallbackDocumentName
  try {
    const result = await readJson(`/api/items/${encodeURIComponent(itemId)}/location`)
    documentName = result.documentName
    shell.showItemInFolder(result.path)
    await logOperation({ event: 'file.show_underlying', message: '在文件管理器中显示底层文件', documentName, result: 'success', durationMs: performance.now() - started })
  } catch (error) {
    await logOperation({ event: 'file.show_underlying', message: '在文件管理器中显示底层文件', documentName, result: 'failure', error: error instanceof Error ? error.message : String(error), durationMs: performance.now() - started })
    if (notificationFromError(error).level === 'fatal') return false
    mainWindow?.webContents.send('business-error', { ...userError(error), title: '打开底层位置失败', msg: '无法打开文件所在位置，请检查文件是否存在及访问权限', errorType: 'location_failed' })
  }
}

function showContextMenu(event, payload) {
  const targetWindow = BrowserWindow.fromWebContents(event.sender)
  if (!targetWindow || !payload || !Number.isInteger(payload.id)) return
  const isFolder = payload.kind === 'folder'
  const action = (name) => () => sendRendererAction({ name, ...payload })
  const template = isFolder
    ? [
        { label: '打开文件夹', click: action('open-folder') },
        { label: '新建子文件夹', click: action('create-folder') },
        { label: '重命名文件夹', click: action('rename-folder') },
        { label: '移动', click: action('move-folder') },
        {
          label: '排序方式',
          submenu: [
            { label: '标准', type: 'radio', click: action('sort-standard') },
            { label: '序列', type: 'radio', click: action('sort-series') }
          ]
        },
        { type: 'separator' },
        { label: '删除文件夹', click: action('delete-folder') }
      ]
    : [
        { label: '用系统默认方式打开', click: () => openItem(payload.id, payload.name) },
        { label: '打开底层位置', click: () => showItemLocation(payload.id, payload.name) },
        { label: '复制文件名', click: () => clipboard.writeText(path.parse(payload.name || '').name) },
        { type: 'separator' },
        { label: '删除', click: action('delete-item') },
        { label: '删除底层文件', click: action('delete-underlying') },
        { label: '重命名', click: action('rename-item') },
        { label: '移动', click: action('move-item') },
        { type: 'separator' },
        { label: '镜像到文件夹', click: action('mirror-item') },
        { label: '替换底层文件', click: action('replace-document') }
      ]
  Menu.buildFromTemplate(template).popup({ window: targetWindow })
}

function installApplicationMenu() {
  const action = (name) => () => sendRendererAction({ name })
  const template = [
    {
      label: '添加',
      submenu: [
        { label: '导入文件', click: action('import-file') },
        { label: '添加文件夹', click: action('add-folder') }
      ]
    },
    { label: '新建文件夹', click: action('create-folder') },
    {
      label: '打开',
      submenu: [
        { label: '打开文件', click: action('open-selected') },
        { label: '打开底层', click: action('show-location-selected') }
      ]
    },
    { label: '按规则重命名', click: action('rename-selected') },
    { label: '保存打开缓存改动', click: action('save-text') },
    { label: '爬取系列', click: action('crawl') },
    {
      label: '设置',
      submenu: [
        { label: '数据位置', click: action('data-location') },
        { label: '缓存管理', click: action('cache-settings') }
      ]
    },
    {
      label: '帮助',
      submenu: [
        { label: '功能介绍', click: action('help') },
        { label: '快捷键', click: action('shortcuts') }
      ]
    },
    { label: '搜索', click: action('search') }
  ]
  const applicationMenu = Menu.buildFromTemplate(template)
  Menu.setApplicationMenu(applicationMenu)
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.setMenu(applicationMenu)
}

ipcMain.handle('bridge:tree', () => readJson('/api/tree'))
ipcMain.handle('bridge:folder', (_event, folderId) => readJson(`/api/folders/${encodeURIComponent(folderId)}/contents`))
ipcMain.handle('bridge:document-url', (_event, documentId) => {
  if (!bridgeUrl) throw new Error('BookManager bridge is not ready')
  return documentUrl(documentId)
})
ipcMain.handle('ui:double-click-delay', () => readDoubleClickDelayMs(systemPreferences))
ipcMain.handle('bridge:open-document', (_event, itemId, documentName) => openItem(itemId, documentName))
ipcMain.handle('bridge:show-location', (_event, itemId, documentName) => showItemLocation(itemId, documentName))
ipcMain.handle('clipboard:copy-file-name', (_event, name) => clipboard.writeText(path.parse(String(name || '')).name))
ipcMain.handle('clipboard:read-text', () => clipboard.readText())
ipcMain.handle('clipboard:write-text', (_event, text) => clipboard.writeText(String(text || '')))
ipcMain.handle('bridge:get', (_event, pathname) => {
  assertAllowedBridgeRequest('GET', pathname)
  return readJson(pathname)
})
ipcMain.handle('bridge:post', (_event, pathname, payload) => {
  assertAllowedBridgeRequest('POST', pathname, payload || {})
  return postJson(pathname, payload || {})
})
ipcMain.handle('dialog:select-file', async () => {
  const result = await dialog.showOpenDialog(mainWindow, { title: '选择要导入的文件', properties: ['openFile'] })
  return result.canceled ? null : result.filePaths[0]
})
ipcMain.handle('dialog:select-folder', async (_event, title = '选择文件夹') => {
  const result = await dialog.showOpenDialog(mainWindow, { title: String(title), properties: ['openDirectory', 'createDirectory'] })
  return result.canceled ? null : result.filePaths[0]
})
ipcMain.on('ui:context-menu', showContextMenu)

if (primaryInstance) app.whenReady().then(async () => {
  try {
    app.setName('BookManager')
    const diagnosticsHome = path.join(app.getPath('userData'), 'diagnostics')
    process.env.BOOKMANAGER_DIAGNOSTIC_HOME = diagnosticsHome
    const originalError = console.error.bind(console)
    const persistError = createMainLogger(diagnosticsHome, { token })
    console.error = (...values) => { persistError(...values); originalError(...values) }
    process.on('uncaughtException', error => { console.error('Uncaught main-process error:', error); reportFatal('main', '程序异常', '程序发生严重错误，请保存可保存的内容并重启软件') })
    process.on('unhandledRejection', error => { console.error('Unhandled main-process rejection:', error); reportFatal('main', '程序异常', '程序发生严重错误，请保存可保存的内容并重启软件') })
    await checkFrontend(staticDir)
    if (process.platform === 'darwin' && app.dock) app.dock.setIcon(appIcon)
    await startBridge()
    protocol.handle(DOCUMENT_SCHEME, createDocumentProtocolHandler(async (documentId, range) => {
      const upstream = await bridgeFetch(
        `/api/documents/${encodeURIComponent(documentId)}/content`,
        range ? { headers: { Range: range } } : {}
      )
      const headers = new Headers(upstream.headers)
      headers.delete('cross-origin-resource-policy')
      headers.set('access-control-allow-origin', bridgeUrl)
      return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers })
    }))
    session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))
    installApplicationMenu()
    createWindow()
  } catch (error) {
    console.error(error)
    const failure = notificationFromError(error)
    const startup = failure.level === 'fatal' ? failure : notificationFromError(startupFailure())
    reportFatal('startup', startup.title, startup.msg)
    app.quit()
  }
})

app.on('window-all-closed', () => app.quit())
app.on('before-quit', (event) => {
  if (quitting) {
    if (bridge && !bridge.killed) bridge.kill()
    return
  }
  if (bridge && !bridge.killed) {
    event.preventDefault()
    quitting = true
    readJson('/api/shutdown')
      .catch((error) => console.error(`BookManager shutdown: ${error}`))
      .finally(() => app.quit())
  }
})
