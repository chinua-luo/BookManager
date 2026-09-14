import { defineConfig } from 'vite'
import { fileViewerRenderers } from '@file-viewer/vite-plugin'

export default defineConfig({
  root: 'src',
  base: './',
  plugins: [fileViewerRenderers({
    formats: ['pdf', 'docx', 'xlsx', 'pptx', 'epub', 'image', 'text'],
    autoPresets: false,
    inject: false,
    copyAssets: true
  })],
  build: {
    outDir: '../dist',
    emptyOutDir: true
  }
})
