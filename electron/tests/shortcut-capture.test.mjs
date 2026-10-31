import assert from 'node:assert/strict'
import test from 'node:test'

import { acceleratorFromKeyboardEvent } from '../src/shortcut-capture.js'

test('Escape, Tab and pure modifier presses are never persisted', () => {
  for (const key of ['Escape', 'Tab', 'Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'CapsLock']) {
    assert.equal(acceleratorFromKeyboardEvent({ key }), null, key)
  }
})

test('auto-repeat and IME composition cannot overwrite a shortcut', () => {
  assert.equal(acceleratorFromKeyboardEvent({ key: 'k', repeat: true }), null)
  assert.equal(acceleratorFromKeyboardEvent({ key: 'k', isComposing: true }), null)
  assert.equal(acceleratorFromKeyboardEvent({}), null)
})

test('regular keys keep the existing accelerator format', () => {
  assert.equal(acceleratorFromKeyboardEvent({ key: 'k' }), 'K')
  assert.equal(acceleratorFromKeyboardEvent({ key: 'k', ctrlKey: true, shiftKey: true }), 'CommandOrControl+Shift+K')
  assert.equal(acceleratorFromKeyboardEvent({ key: 'k', metaKey: true, altKey: true }), 'CommandOrControl+Alt+K')
  assert.equal(acceleratorFromKeyboardEvent({ key: 'F5' }), 'F5')
  assert.equal(acceleratorFromKeyboardEvent({ key: 'ArrowUp' }), 'ArrowUp')
})

test('Backspace is the explicit clear gesture', () => {
  assert.equal(acceleratorFromKeyboardEvent({ key: 'Backspace' }), '')
  assert.equal(acceleratorFromKeyboardEvent({ key: 'Backspace', repeat: true }), null)
})
