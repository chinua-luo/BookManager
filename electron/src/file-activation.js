export const DEFAULT_CLICK_DELAY_MS = 600

export function bindFileActivation(
  element,
  { select = () => {}, preview, open, onError = () => {}, delayMs = DEFAULT_CLICK_DELAY_MS },
  timers = globalThis
) {
  let pendingPreview = null

  const cancelPendingPreview = () => {
    if (pendingPreview === null) return
    timers.clearTimeout(pendingPreview)
    pendingPreview = null
  }

  const run = callback => {
    try {
      Promise.resolve(callback()).catch(onError)
    } catch (error) {
      onError(error)
    }
  }

  const handleClick = event => {
    select()
    cancelPendingPreview()
    if (event.detail === 0) {
      run(preview)
      return
    }
    if (event.detail !== 1) return
    pendingPreview = timers.setTimeout(() => {
      pendingPreview = null
      run(preview)
    }, delayMs)
  }

  const handleDoubleClick = event => {
    event.preventDefault()
    cancelPendingPreview()
    run(open)
  }

  element.addEventListener('click', handleClick)
  element.addEventListener('dblclick', handleDoubleClick)

  return () => {
    cancelPendingPreview()
    element.removeEventListener('click', handleClick)
    element.removeEventListener('dblclick', handleDoubleClick)
  }
}
