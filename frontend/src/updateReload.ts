/**
 * Self-reload after an update: the decision logic, kept apart from React so it can be tested
 * without a browser (`node --test frontend/tests/`).
 *
 * Rule (nobody should have to reload by hand, and a draft must never be lost):
 *   - NEVER reload while an unsent draft exists (a text field with content, a chosen file), while a
 *     field has focus, or while a writing request / upload is in flight.
 *   - Otherwise: visible tab -> only after a quiet period (nothing typed, tapped or scrolled); when the
 *     tab goes to the background and when it comes back, immediately.
 */

export type PageState = {
  visible: boolean
  idleMs: number          // time since the user's last input
  focusInField: boolean   // typing right now (focus in an input / select / contenteditable)
  draft: boolean          // unsent content or a chosen file anywhere on the page
  pendingWrites: number   // writing requests (POST/PUT/DELETE, uploads) in flight
}

export const IDLE_MS = 15000

/** May the page reload itself now? `immediate` = tab went to the background / came back. */
export function reloadAllowed(s: PageState, opt: { immediate?: boolean; idleMs?: number } = {}): boolean {
  if (s.draft || s.focusInField || s.pendingWrites > 0) return false
  if (opt.immediate) return true
  if (s.visible && s.idleMs < (opt.idleMs ?? IDLE_MS)) return false
  return true
}

const NON_TEXT = new Set(['hidden', 'checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'image'])

type FieldLike = { tagName: string; type?: string; value?: string; files?: { length: number } | null; isContentEditable?: boolean; textContent?: string | null }
type DocLike = { querySelectorAll(sel: string): ArrayLike<FieldLike>; activeElement?: FieldLike | null }

/** Unsent content anywhere on the page? (text fields with text, chosen files, contenteditable with text) */
export function hasDraft(doc: DocLike): boolean {
  for (const el of Array.from(doc.querySelectorAll('textarea, input, [contenteditable="true"]'))) {
    const tag = (el.tagName || '').toLowerCase()
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase()
      if (t === 'file') { if (el.files && el.files.length > 0) return true; continue }
      if (NON_TEXT.has(t)) continue
    }
    if (tag === 'textarea' || tag === 'input') { if ((el.value || '').trim() !== '') return true; continue }
    if ((el.textContent || '').trim() !== '') return true   // contenteditable
  }
  return false
}

/** Does an input field have focus right now? */
export function focusInField(doc: DocLike): boolean {
  const a = doc.activeElement
  if (!a) return false
  const tag = (a.tagName || '').toLowerCase()
  if (tag === 'textarea' || tag === 'select' || a.isContentEditable) return true
  if (tag === 'input') return !NON_TEXT.has((a.type || 'text').toLowerCase())
  return false
}

/** One self-reload per new version is enough: if the same hash comes back after the reload (stale cache), do not loop. */
export function loopGuard(store: { getItem(k: string): string | null }, next: string): boolean {
  return store.getItem('aiw_autoreload') === next
}
