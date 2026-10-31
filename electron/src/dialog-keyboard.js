// Pure decisions for the dialog-level keyboard handler. Keeping them here makes
// the focus-trap, IME and textarea rules testable without a browser DOM.
export function isTextareaTarget(target) {
  return String(target?.tagName || '').toUpperCase() === 'TEXTAREA'
}

export function dialogKeyAction({ key = '', target = null, isComposing = false, shiftKey = false, busy = false } = {}) {
  if (key === 'Escape') return busy ? null : 'close'
  if (key === 'Enter') {
    if (isComposing || isTextareaTarget(target) || target?.readOnly === true) return null
    // Let Enter activate the focused button natively instead of always submitting.
    if (String(target?.tagName || '').toUpperCase() === 'BUTTON') return null
    return 'submit'
  }
  if (key === 'Tab') return shiftKey ? 'focus-last-or-previous' : 'focus-first-or-next'
  return null
}

// Returns the dialog control index that must receive focus, or -1 when the
// browser's native Tab handling can continue (the focus stays inside the panel).
export function focusTrapTarget(count, activeIndex, shiftKey) {
  if (!Number.isInteger(count) || count <= 0) return -1
  if (!Number.isInteger(activeIndex) || activeIndex < 0 || activeIndex >= count) {
    return shiftKey ? count - 1 : 0
  }
  if (shiftKey && activeIndex === 0) return count - 1
  if (!shiftKey && activeIndex === count - 1) return 0
  return -1
}

export function isContextMenuKey(event = {}) {
  return event.key === 'ContextMenu' || (event.shiftKey === true && event.key === 'F10')
}
