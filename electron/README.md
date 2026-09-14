# BookManager Electron Preview

This is the first Electron interface migration. It retains the Python `LibraryStore`,
SQLite database, SHA-256 Blob store, naming, import, and crawler logic.

The Electron main process starts `bookmanager.bridge` on a random loopback port. The
renderer can request folder and item data, and it can load a document only by
`document_id`. The bridge resolves the current Blob internally and never returns a
filesystem path to the renderer.

The preview build includes only these File Viewer renderers:

- PDF
- EPUB
- DOCX / Word
- XLSX / spreadsheet
- PPTX
- Common images and text

DJVU remains a Python-side fallback. The bridge renders its first two pages using
the existing PyMuPDF/DjVuLibre path and passes only the rendered page images to the
Electron renderer.

## Development

Run from this directory with a system Node.js installation:

```bash
pnpm install
pnpm start
```

`pnpm start` builds the frontend and opens Electron. It uses `python3` on macOS/Linux
and `py -3` on Windows. Set `BOOKMANAGER_PYTHON` to override the Python executable.

The bridge follows the data location selected by the existing Python application.
Set `BOOKMANAGER_DATA_HOME` only when testing a different library location.
