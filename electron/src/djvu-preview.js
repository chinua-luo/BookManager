const DJVU_EXTENSIONS = new Set(['.djvu', '.djv'])

export function isDjvuExtension(extension) {
  return DJVU_EXTENSIONS.has(String(extension || '').toLowerCase())
}

export function createDjvuPreview(host, djvuGlobal = globalThis.DjVu) {
  let viewer = null

  function getViewer() {
    if (viewer) return viewer
    if (!djvuGlobal?.Viewer) {
      throw new Error('DJVU 预览组件没有正确加载')
    }
    viewer = new djvuGlobal.Viewer()
    viewer.render(host)
    return viewer
  }

  return {
    async load(url, name) {
      const instance = getViewer()
      await instance.loadDocumentByUrl(url, {
        name,
        viewMode: 'continuous',
        language: 'zh',
        theme: 'light',
        uiOptions: {
          hideFullPageSwitch: true,
          hideOpenAndCloseButtons: true,
          hidePrintButton: true,
          hideSaveButton: true,
          showContentsAutomatically: false
        }
      })
    }
  }
}
