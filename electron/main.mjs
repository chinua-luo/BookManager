import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, session, shell } from 'electron'
import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const electronDir = path.dirname(fileURLToPath(import.meta.url))
const projectDir = process.env.BOOKMANAGER_PROJECT_DIR || path.resolve(electronDir, '..')
const staticDir = path.join(projectDir, 'electron', 'dist')
const appIcon = path.join(projectDir, 'book.png')
const token = randomBytes(32).toString('hex')
let bridge = null
let bridgeUrl = null
let mainWindow = null
let quitting = false

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
    headers: { 'X-BookManager-Token': token, ...(options.headers || {}) }
  })
}

function startBridge() {
  const command = pythonCommand()
  const args = [
    ...command.prefix,
    '-m', 'bookmanager.bridge',
    '--port', '0',
    '--token', token,
    '--static-dir', staticDir
  ]
  if (process.env.BOOKMANAGER_DATA_HOME) {
    args.push('--data-home', process.env.BOOKMANAGER_DATA_HOME)
  }
  bridge = spawn(command.executable, args, { cwd: projectDir, stdio: ['ignore', 'pipe', 'pipe'] })
  bridge.stderr.on('data', (chunk) => console.error(`BookManager bridge: ${chunk}`))
  return new Promise((resolve, reject) => {
    let output = ''
    const timer = setTimeout(() => reject(new Error('BookManager bridge did not start in time')), 10000)
    bridge.stdout.on('data', (chunk) => {
      output += chunk.toString()
      const lines = output.split('\n')
      output = lines.pop()
      for (const line of lines) {
        try {
          const event = JSON.parse(line)
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
      reject(error)
    })
    bridge.once('exit', (code) => {
      if (!bridgeUrl) {
        clearTimeout(timer)
        reject(new Error(`BookManager bridge exited before startup (${code ?? 'unknown'})`))
      }
    })
  })
}

async function readJson(pathname) {
  const response = await bridgeFetch(pathname)
  if (!response.ok) throw new Error(await response.text())
  return response.json()
}

async function postJson(pathname, payload = {}) {
  const response = await bridgeFetch(pathname, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-BookManager-Token': token },
    body: JSON.stringify(payload)
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.error || `请求失败 (${response.status})`)
  }
  return response.json()
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
  mainWindow.loadURL(bridgeUrl)
}

function sendRendererAction(action) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('menu-action', action)
  }
}

async function openItem(itemId) {
  try {
    const result = await readJson(`/api/items/${encodeURIComponent(itemId)}/open`)
    const error = await shell.openPath(result.path)
    if (error) throw new Error(error)
  } catch (error) {
    dialog.showErrorBox('打开文件失败', error instanceof Error ? error.message : String(error))
  }
}

async function showItemLocation(itemId) {
  try {
    const result = await readJson(`/api/items/${encodeURIComponent(itemId)}/location`)
    shell.showItemInFolder(result.path)
  } catch (error) {
    dialog.showErrorBox('打开底层位置失败', error instanceof Error ? error.message : String(error))
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
        { label: '用系统默认方式打开', click: () => openItem(payload.id) },
        { label: '打开底层位置', click: () => showItemLocation(payload.id) },
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
    { label: '保存文本改动', click: action('save-text') },
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
  return `${bridgeUrl}/api/documents/${encodeURIComponent(documentId)}/content?token=${token}`
})
ipcMain.handle('bridge:djvu-page-urls', (_event, documentId) => {
  if (!bridgeUrl) throw new Error('BookManager bridge is not ready')
  const base = `${bridgeUrl}/api/documents/${encodeURIComponent(documentId)}/djvu-pages`
  return [`${base}/1?token=${token}`, `${base}/2?token=${token}`]
})
ipcMain.handle('bridge:epub-preview', (_event, documentId) => readJson(`/api/documents/${encodeURIComponent(documentId)}/epub-preview`))
ipcMain.handle('bridge:epub-page-urls', (_event, documentId) => {
  if (!bridgeUrl) throw new Error('BookManager bridge is not ready')
  const base = `${bridgeUrl}/api/documents/${encodeURIComponent(documentId)}/epub-pages`
  return [`${base}/1?token=${token}`, `${base}/2?token=${token}`]
})
ipcMain.handle('bridge:open-document', (_event, itemId) => openItem(itemId))
ipcMain.handle('bridge:show-location', (_event, itemId) => showItemLocation(itemId))
ipcMain.handle('clipboard:copy-file-name', (_event, name) => clipboard.writeText(path.parse(String(name || '')).name))
ipcMain.handle('clipboard:read-text', () => clipboard.readText())
ipcMain.handle('clipboard:write-text', (_event, text) => clipboard.writeText(String(text || '')))
ipcMain.handle('bridge:get', (_event, pathname) => readJson(String(pathname)))
ipcMain.handle('bridge:post', (_event, pathname, payload) => postJson(String(pathname), payload || {}))
ipcMain.handle('dialog:select-file', async () => {
  const result = await dialog.showOpenDialog(mainWindow, { title: '选择要导入的文件', properties: ['openFile'] })
  return result.canceled ? null : result.filePaths[0]
})
ipcMain.handle('dialog:select-folder', async (_event, title = '选择文件夹') => {
  const result = await dialog.showOpenDialog(mainWindow, { title: String(title), properties: ['openDirectory', 'createDirectory'] })
  return result.canceled ? null : result.filePaths[0]
})
ipcMain.on('ui:context-menu', showContextMenu)

app.whenReady().then(async () => {
  try {
    app.setName('BookManager')
    if (process.platform === 'darwin' && app.dock) app.dock.setIcon(appIcon)
    await startBridge()
    session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))
    installApplicationMenu()
    createWindow()
  } catch (error) {
    console.error(error)
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
