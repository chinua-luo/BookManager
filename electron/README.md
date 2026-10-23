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
- DJVU (DjVu.js, executed locally in Electron)

DJVU uses the vendored DjVu.js library and viewer. It reads the same authenticated
`document_id` URL as the other renderers, decodes in a Web Worker, and presents a
continuous-scroll preview. It does not expose the Blob path and does not require
PyMuPDF or a system DjVuLibre installation for previewing.

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

Third-party browser assets and their licenses are listed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
