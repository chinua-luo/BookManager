export const DOCUMENT_SCHEME = 'bookmanager-document'

export function documentUrl(documentId) {
  const id = Number(documentId)
  if (!Number.isSafeInteger(id) || id <= 0) throw new TypeError('Invalid document id')
  return `${DOCUMENT_SCHEME}://content/${id}`
}

export function documentIdFromUrl(value) {
  const url = new URL(value)
  const id = Number(url.pathname.slice(1))
  if (url.protocol !== `${DOCUMENT_SCHEME}:` || url.hostname !== 'content' || !Number.isSafeInteger(id) || id <= 0) {
    throw new TypeError('Invalid BookManager document URL')
  }
  return id
}

export function createDocumentProtocolHandler(fetchDocument) {
  return (request) => {
    const documentId = documentIdFromUrl(request.url)
    const range = request.headers.get('range')
    return fetchDocument(documentId, range)
  }
}
