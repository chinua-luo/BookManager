const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('bookManager', {
  tree: () => ipcRenderer.invoke('bridge:tree'),
  folder: (folderId) => ipcRenderer.invoke('bridge:folder', folderId),
  documentUrl: (documentId) => ipcRenderer.invoke('bridge:document-url', documentId),
  doubleClickDelay: () => ipcRenderer.invoke('ui:double-click-delay'),
  openDocument: (itemId, documentName) => ipcRenderer.invoke('bridge:open-document', itemId, documentName),
  showLocation: (itemId, documentName) => ipcRenderer.invoke('bridge:show-location', itemId, documentName),
  copyFileName: (name) => ipcRenderer.invoke('clipboard:copy-file-name', name),
  clipboardReadText: () => ipcRenderer.invoke('clipboard:read-text'),
  clipboardWriteText: (text) => ipcRenderer.invoke('clipboard:write-text', text),
  get: (pathname) => ipcRenderer.invoke('bridge:get', pathname),
  post: (pathname, payload) => ipcRenderer.invoke('bridge:post', pathname, payload),
  selectFile: () => ipcRenderer.invoke('dialog:select-file'),
  selectFolder: (title) => ipcRenderer.invoke('dialog:select-folder', title),
  showContextMenu: (payload) => ipcRenderer.send('ui:context-menu', payload),
  onBusinessError: (callback) => ipcRenderer.on('business-error', (_event, notice) => callback(notice)),
  onMenuAction: (callback) => ipcRenderer.on('menu-action', (_event, action) => callback(action))
})
