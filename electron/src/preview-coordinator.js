export function createPreviewCoordinator({ keyOf, load }) {
  let version = 0
  let activeKey = null
  let pendingKey = null

  return {
    async show(item) {
      const key = keyOf(item)
      if (key === pendingKey) return false
      if (key === activeKey) {
        if (pendingKey !== null) {
          version += 1
          pendingKey = null
        }
        return false
      }

      const requestVersion = ++version
      pendingKey = key
      const isCurrent = () => requestVersion === version
      try {
        await load(item, isCurrent)
        if (!isCurrent()) return false
        activeKey = key
        pendingKey = null
        return true
      } catch (error) {
        if (!isCurrent()) return false
        pendingKey = null
        throw error
      }
    },

    invalidate() {
      version += 1
      activeKey = null
      pendingKey = null
    }
  }
}
