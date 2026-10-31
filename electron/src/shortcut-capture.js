// Translate a keydown event into a persisted accelerator. Escape, Tab, pure
// modifier presses, IME composition and auto-repeat are never saved.
export const MODIFIER_KEYS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'OS', 'AltGraph', 'CapsLock', 'Dead'])
export const IGNORED_KEYS = new Set(['Escape', 'Tab'])

export function acceleratorFromKeyboardEvent(event = {}) {
  const { key, ctrlKey = false, metaKey = false, altKey = false, shiftKey = false, repeat = false, isComposing = false } = event
  if (repeat || isComposing || !key) return null
  if (IGNORED_KEYS.has(key) || MODIFIER_KEYS.has(key)) return null
  if (key === 'Backspace') return ''
  const modifier = ctrlKey || metaKey ? 'CommandOrControl+' : ''
  const alt = altKey ? 'Alt+' : ''
  const shift = shiftKey ? 'Shift+' : ''
  return `${modifier}${alt}${shift}${key.length === 1 ? key.toUpperCase() : key}`
}
