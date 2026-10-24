// One visible dialog, with suspended parents for form-owned confirmations.
export function createModalManager({ now = Date.now, defer = callback => setTimeout(callback, 0), log = console.warn } = {}) {
  let active = null
  let scheduled = false
  const queue = []
  const parents = []
  const recent = new Map()
  const pending = new Set()
  function advance() {
    if (scheduled) return
    scheduled = true
    defer(() => {
      scheduled = false
      if (active) return
      active = parents.pop() || queue.shift() || null
      active?.show()
    })
  }
  return {
    show(entry, { key = '', dedup = false, nested = false } = {}) {
      const time = now()
      for (const [type, timestamp] of recent) if (time - timestamp >= 3000) recent.delete(type)
      if (key && (pending.has(key) || (dedup && recent.has(key)))) {
        log(`[modal dedup] ${key}`)
        entry.dismiss?.()
        return () => {}
      }
      if (key) { pending.add(key); if (dedup) recent.set(key, time) }
      const item = { ...entry, key }
      if (nested && active && active.allowChild !== false) { active.hide(); parents.push(active); active = null }
      if (!active && !scheduled) { active = item; item.show() } else queue.push(item)
      let closed = false
      return () => {
        if (closed) return
        closed = true
        if (key) pending.delete(key)
        const wasActive = active === item
        if (wasActive) { active = null; advance() }
        item.dismiss?.()
        if (!wasActive) {
          for (const list of [queue, parents]) { const index = list.indexOf(item); if (index >= 0) list.splice(index, 1) }
        }
      }
    }
  }
}
