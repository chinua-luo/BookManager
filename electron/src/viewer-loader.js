// Lazily import the file viewer and its renderers. Text and DJVU previews never
// touch this module, so the heavy renderer bundles are only downloaded and
// evaluated the first time a PDF/Word/EPUB/image preview is requested.
const importViewerModule = () => import('@file-viewer/web')
const importRendererModules = () => Promise.all([
  import('@file-viewer/renderer-pdf'),
  import('@file-viewer/renderer-word'),
  import('@file-viewer/renderer-spreadsheet'),
  import('@file-viewer/renderer-pptx'),
  import('@file-viewer/renderer-epub'),
  import('@file-viewer/renderer-image'),
  import('@file-viewer/renderer-text')
]).then(modules => modules.map(module => module.default))

export function createViewerLoader({
  importViewer = importViewerModule,
  importRenderers = importRendererModules
} = {}) {
  let pending = null
  return {
    load() {
      if (!pending) {
        pending = Promise.all([
          Promise.resolve().then(importViewer),
          Promise.resolve().then(importRenderers)
        ]).then(([viewerModule, renderers]) => ({ mountViewer: viewerModule.mountViewer, renderers }))
        // A failed import must be retryable instead of cache-poisoning the app.
        pending.catch(() => { pending = null })
      }
      return pending
    }
  }
}

export const viewerLoader = createViewerLoader()
