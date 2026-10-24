export function recursiveEnabled(checked, confirmed) {
  return checked === true && confirmed === true
}

export function crawlRequestPayload(source, folderId, checked, confirmed) {
  return {
    source,
    folderId,
    enable_recursive: recursiveEnabled(checked, confirmed)
  }
}
