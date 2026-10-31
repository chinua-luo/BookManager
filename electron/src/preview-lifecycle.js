// Viewer resources and text fetches must be released when the preview changes.
// Both helpers are dependency-injected so they can be unit tested under node:test.
export function waitForLayout({
  isCurrent = () => true,
  measure,
  schedule = callback => requestAnimationFrame(callback),
  cancel = null,
  now = () => Date.now(),
  timeoutMs = 5000
} = {}) {
  return new Promise(resolve => {
    const startedAt = now()
    let frame = null
    let finished = false
    const finish = value => {
      if (finished) return
      finished = true
      if (frame !== null && cancel) cancel(frame)
      resolve(value)
    }
    const check = () => {
      if (!isCurrent()) { finish(false); return }
      const bounds = measure()
      if (bounds && bounds.width > 0 && bounds.height > 0) { finish(true); return }
      if (now() - startedAt >= timeoutMs) { finish(false); return }
      frame = schedule(check)
    }
    check()
  })
}

export function createPreviewResourceManager({
  unloadViewer = () => {},
  abortText = controller => controller.abort(),
  createAbortController = () => new AbortController()
} = {}) {
  let kind = null
  let textController = null
  let releaseChain = Promise.resolve()
  const abortPendingText = () => {
    if (!textController) return
    const controller = textController
    textController = null
    abortText(controller)
  }
  // Unloads are queued so a new document load cannot interleave with the
  // teardown of the previous one.
  const queueUnload = () => {
    releaseChain = releaseChain.then(() => unloadViewer('source-changed')).catch(() => {})
  }
  return {
    get kind() { return kind },
    begin(nextKind) {
      if (kind === 'viewer') queueUnload()
      abortPendingText()
      kind = nextKind
    },
    beginText() {
      abortPendingText()
      const controller = createAbortController()
      textController = controller
      return controller
    },
    settleText(controller) {
      if (textController === controller) textController = null
    },
    release() {
      if (kind === 'viewer') queueUnload()
      kind = null
      abortPendingText()
    },
    async settle() { await releaseChain }
  }
}
