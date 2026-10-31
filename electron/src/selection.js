// Pure row-navigation helper shared by the folder tree and the content list.
export const NAVIGATION_KEYS = new Set(['ArrowUp', 'ArrowDown'])

export function isNavigationKey(key) {
  return NAVIGATION_KEYS.has(key)
}

export function nextRowIndex(length, currentIndex, key) {
  if (!Number.isInteger(length) || length <= 0) return -1
  if (!NAVIGATION_KEYS.has(key)) return Number.isInteger(currentIndex) && currentIndex >= 0 && currentIndex < length ? currentIndex : -1
  if (!Number.isInteger(currentIndex) || currentIndex < 0 || currentIndex >= length) {
    return key === 'ArrowDown' ? 0 : length - 1
  }
  const delta = key === 'ArrowDown' ? 1 : -1
  return Math.max(0, Math.min(length - 1, currentIndex + delta))
}
