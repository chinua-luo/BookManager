import assert from 'node:assert/strict'
import test from 'node:test'

import { dialogKeyAction, focusTrapTarget, isContextMenuKey } from '../src/dialog-keyboard.js'

test('Escape closes dialogs unless a mutation is busy', () => {
  assert.equal(dialogKeyAction({ key: 'Escape' }), 'close')
  assert.equal(dialogKeyAction({ key: 'Escape', busy: true }), null)
})

test('Enter submits except inside textareas, read-only capture fields and IME composition', () => {
  assert.equal(dialogKeyAction({ key: 'Enter', target: { tagName: 'INPUT' } }), 'submit')
  assert.equal(dialogKeyAction({ key: 'Enter', target: { tagName: 'TEXTAREA' } }), null)
  assert.equal(dialogKeyAction({ key: 'Enter', target: { tagName: 'textarea' } }), null)
  assert.equal(dialogKeyAction({ key: 'Enter', isComposing: true }), null)
  assert.equal(dialogKeyAction({ key: 'Enter', target: { tagName: 'INPUT', readOnly: true } }), null)
  assert.equal(dialogKeyAction({ key: 'Enter', target: { tagName: 'BUTTON' } }), null)
})

test('Tab and other keys leave focus movement to the trap or the browser', () => {
  assert.equal(dialogKeyAction({ key: 'Tab' }), 'focus-first-or-next')
  assert.equal(dialogKeyAction({ key: 'Tab', shiftKey: true }), 'focus-last-or-previous')
  assert.equal(dialogKeyAction({ key: 'a' }), null)
})

test('the focus trap wraps at the ends and pulls focus back from outside the dialog', () => {
  assert.equal(focusTrapTarget(3, 2, false), 0)
  assert.equal(focusTrapTarget(3, 0, true), 2)
  assert.equal(focusTrapTarget(3, 1, false), -1)
  assert.equal(focusTrapTarget(3, 1, true), -1)
  assert.equal(focusTrapTarget(4, -1, false), 0)
  assert.equal(focusTrapTarget(4, -1, true), 3)
  assert.equal(focusTrapTarget(4, 4, false), 0)
  assert.equal(focusTrapTarget(0, 0, false), -1)
})

test('the context menu opens with Shift+F10 or the ContextMenu key', () => {
  assert.equal(isContextMenuKey({ key: 'ContextMenu' }), true)
  assert.equal(isContextMenuKey({ key: 'F10', shiftKey: true }), true)
  assert.equal(isContextMenuKey({ key: 'F10', shiftKey: false }), false)
  assert.equal(isContextMenuKey({ key: 'Enter' }), false)
  assert.equal(isContextMenuKey(), false)
})
