// The conversation thread id survives a page reload (sessionStorage: one browser tab, gone when it closes), so the
// agent keeps the earlier turns as context. "새 대화" clears it.

const PREFIX = 'atlas.threadId.'

/** One key per data source mode: a mock thread id must never reach the live backend. */
export const threadStorageKey = (mode: string): string => `${PREFIX}${mode}`

function sessionStore(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.sessionStorage
  } catch {
    // Storage can be blocked (privacy settings); the thread is then simply not kept.
    return null
  }
}

export function loadThreadId(key: string, store: Pick<Storage, 'getItem'> | null = sessionStore()): string | null {
  try {
    const value = store?.getItem(key)
    return value && value.trim() ? value : null
  } catch {
    return null
  }
}

/** Store the thread id, or forget it for `null`. */
export function saveThreadId(key: string, threadId: string | null, store: Pick<Storage, 'setItem' | 'removeItem'> | null = sessionStore()): void {
  try {
    if (threadId) store?.setItem(key, threadId)
    else store?.removeItem(key)
  } catch {
    // quota or blocked storage: ignore
  }
}
