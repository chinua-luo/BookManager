const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('bookManager', {
  tree: () => ipcRenderer.invoke('bridge:tree'),
  folder: (folderId) => ipcRenderer.invoke('bridge:folder', folderId),
  documentUrl: (documentId) => ipcRenderer.invoke('bridge:document-url', documentId),
  djvuPageUrls: (documentId) => ipcRenderer.invoke('bridge:djvu-page-urls', documentId),
  epubPreview: (documentId) => ipcRenderer.invoke('bridge:epub-preview', documentId),
  epubPageUrls: (documentId) => ipcRenderer.invoke('bridge:epub-page-urls', documentId),
  openDocument: (itemId) => ipcRenderer.invoke('bridge:open-document', itemId),
  showLocation: (itemId) => ipcRenderer.invoke('bridge:show-location', itemId),
  copyFileName: (name) => ipcRenderer.invoke('clipboard:copy-file-name', name),
  clipboardReadText: () => ipcRenderer.invoke('clipboard:read-text'),
  clipboardWriteText: (text) => ipcRenderer.invoke('clipboard:write-text', text),
  get: (pathname) => ipcRenderer.invoke('bridge:get', pathname),
  post: (pathname, payload) => ipcRenderer.invoke('bridge:post', pathname, payload),
  selectFile: () => ipcRenderer.invoke('dialog:select-file'),
  selectFolder: (title) => ipcRenderer.invoke('dialog:select-folder', title),
  showContextMenu: (payload) => ipcRenderer.send('ui:context-menu', payload),
  onMenuAction: (callback) => ipcRenderer.on('menu-action', (_event, action) => callback(action))
})
