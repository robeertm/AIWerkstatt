// Self-reload test: node --test frontend/tests/
// Pins the rule: draft / focus / pending write -> NEVER reload; otherwise after idle time, or at once on hide/return.
import test from 'node:test'
import assert from 'node:assert/strict'
import { reloadAllowed, hasDraft, focusInField, loopGuard, IDLE_MS } from '../src/updateReload.ts'

const calm = { visible: true, idleMs: 60000, focusInField: false, draft: false, pendingWrites: 0 }
const doc = (fields, active = null) => ({ querySelectorAll: () => fields, activeElement: active })
const ta = (value) => ({ tagName: 'TEXTAREA', value })
const inp = (type, value = '', files = null) => ({ tagName: 'INPUT', type, value, files })

test('a calm page reloads after the idle time', () => assert.equal(reloadAllowed(calm), true))
test('not shortly after an input (visible tab)', () => assert.equal(reloadAllowed({ ...calm, idleMs: IDLE_MS - 1 }), false))
test('in the background the idle time does not count', () => assert.equal(reloadAllowed({ ...calm, visible: false, idleMs: 0 }), true))
test('hide/return (immediate) reloads without idle time', () => assert.equal(reloadAllowed({ ...calm, idleMs: 0 }, { immediate: true }), true))
test('a draft ALWAYS blocks, also when immediate and in the background', () => {
  for (const immediate of [false, true]) for (const visible of [true, false])
    assert.equal(reloadAllowed({ ...calm, visible, draft: true }, { immediate }), false)
})
test('focus in a field ALWAYS blocks', () => {
  for (const immediate of [false, true]) assert.equal(reloadAllowed({ ...calm, focusInField: true }, { immediate }), false)
})
test('a pending upload/POST ALWAYS blocks', () => {
  for (const immediate of [false, true]) assert.equal(reloadAllowed({ ...calm, pendingWrites: 1 }, { immediate }), false)
})

test('detect a draft: text, whitespace only, file, password', () => {
  assert.equal(hasDraft(doc([])), false)
  assert.equal(hasDraft(doc([ta('')])), false)
  assert.equal(hasDraft(doc([ta('   \n')])), false)
  assert.equal(hasDraft(doc([ta('half typed')])), true)
  assert.equal(hasDraft(doc([inp('text', 'Title')])), true)
  assert.equal(hasDraft(doc([inp('password', '1234')])), true)
  assert.equal(hasDraft(doc([inp('file', '', { length: 1 })])), true)
  assert.equal(hasDraft(doc([inp('file', '', { length: 0 })])), false)
  assert.equal(hasDraft(doc([inp('checkbox', 'on'), inp('hidden', 'x'), inp('submit', 'Send')])), false)
  assert.equal(hasDraft(doc([{ tagName: 'DIV', isContentEditable: true, textContent: 'Note' }])), true)
})
test('detect focus', () => {
  assert.equal(focusInField(doc([], null)), false)
  assert.equal(focusInField(doc([], { tagName: 'BUTTON' })), false)
  assert.equal(focusInField(doc([], { tagName: 'TEXTAREA' })), true)
  assert.equal(focusInField(doc([], inp('text'))), true)
  assert.equal(focusInField(doc([], inp('checkbox'))), false)
  assert.equal(focusInField(doc([], { tagName: 'SELECT' })), true)
})
test('loop guard: never reload into the same version twice', () => {
  const m = { a: null }; const store = { getItem: () => m.a }
  assert.equal(loopGuard(store, 'abc'), false)
  m.a = 'abc'
  assert.equal(loopGuard(store, 'abc'), true)
  assert.equal(loopGuard(store, 'def'), false)
})
