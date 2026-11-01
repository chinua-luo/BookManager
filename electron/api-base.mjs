export const BRIDGE_NOT_READY_MESSAGE = 'BookManager bridge is not ready'
export const BRIDGE_NOT_READY_MARKER = 'bridge.api_before_ready'

/**
 * Build the single local API base resolver used by main-process HTTP requests.
 *
 * The backend publishes its dynamic loopback port only through the bridge
 * `ready` event, so `readBase` must return that URL or a falsy value. Resolution
 * is synchronous: callers get the current base or an immediate error, never a
 * queued or deferred request. The first attempt before readiness records one
 * diagnostic marker through the supplied log (main's console.error handler);
 * later attempts throw without logging again.
 *
 * @param {() => string | null | undefined} readBase
 * @param {{ log?: (...values: unknown[]) => void }} [options]
 * @returns {() => string}
 */
export function createApiBaseResolver(readBase, { log = (...values) => console.error(...values) } = {}) {
  if (typeof readBase !== 'function') throw new TypeError('readBase must be a function')
  let reported = false
  return function resolveApiBase() {
    const base = readBase()
    if (typeof base === 'string' && base.length > 0) return base
    if (!reported) {
      reported = true
      log(`${BRIDGE_NOT_READY_MARKER}: ${BRIDGE_NOT_READY_MESSAGE}`)
    }
    throw new Error(BRIDGE_NOT_READY_MESSAGE)
  }
}
