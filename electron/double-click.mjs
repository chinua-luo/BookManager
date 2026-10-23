const FALLBACK_DOUBLE_CLICK_INTERVAL_MS = 500
const DOUBLE_CLICK_GRACE_MS = 100
const MIN_DOUBLE_CLICK_INTERVAL_MS = 250
const MAX_DOUBLE_CLICK_INTERVAL_MS = 1400

export function resolveDoubleClickDelayMs(value) {
  const milliseconds = Number(value) * 1000
  const interval = Number.isFinite(milliseconds) && milliseconds > 0
    ? milliseconds
    : FALLBACK_DOUBLE_CLICK_INTERVAL_MS
  return Math.round(
    Math.max(MIN_DOUBLE_CLICK_INTERVAL_MS, Math.min(MAX_DOUBLE_CLICK_INTERVAL_MS, interval))
      + DOUBLE_CLICK_GRACE_MS
  )
}

export function readDoubleClickDelayMs(systemPreferences, platform = process.platform) {
  if (platform !== 'darwin' || !systemPreferences?.getUserDefault) {
    return resolveDoubleClickDelayMs(null)
  }
  try {
    return resolveDoubleClickDelayMs(
      systemPreferences.getUserDefault('com.apple.mouse.doubleClickThreshold', 'double')
    )
  } catch {
    return resolveDoubleClickDelayMs(null)
  }
}
