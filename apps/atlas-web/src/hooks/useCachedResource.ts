import { useCallback, useEffect, useRef, useState } from 'react'

// Module-level promise cache shared by all components (keys must include the data-source mode).
const cache = new Map<string, Promise<unknown>>()

export function invalidateCache(prefix: string): void {
  for (const key of [...cache.keys()]) if (key.startsWith(prefix)) cache.delete(key)
}

export function clearCache(): void {
  cache.clear()
}

/** Fetch through the shared cache outside of React (failed promises are evicted). */
export function cachedFetch<T>(key: string, loader: () => Promise<T>): Promise<T> {
  let promise = cache.get(key) as Promise<T> | undefined
  if (!promise) {
    promise = loader()
    cache.set(key, promise)
    promise.catch(() => cache.delete(key))
  }
  return promise
}

export interface ResourceState<T> {
  data: T | undefined
  error: Error | undefined
  loading: boolean
  reload: () => void
}

interface Snapshot<T> {
  key: string | null
  data?: T
  error?: Error
  loading: boolean
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error))
}

/** Load (and cache) an async resource by key; `null` key means "nothing to load". */
export function useCachedResource<T>(key: string | null, loader: () => Promise<T>): ResourceState<T> {
  const [snapshot, setSnapshot] = useState<Snapshot<T>>({ key: null, loading: false })
  const [nonce, setNonce] = useState(0)
  const loaderRef = useRef(loader)

  useEffect(() => {
    loaderRef.current = loader
  })

  useEffect(() => {
    if (key === null) {
      setSnapshot({ key: null, loading: false })
      return
    }
    let cancelled = false
    const promise = cachedFetch(key, () => loaderRef.current())
    setSnapshot((prev) => ({ key, data: prev.key === key ? prev.data : undefined, loading: true }))
    promise.then(
      (data) => {
        if (!cancelled) setSnapshot({ key, data, loading: false })
      },
      (error: unknown) => {
        if (!cancelled) setSnapshot({ key, error: toError(error), loading: false })
      },
    )
    return () => {
      cancelled = true
    }
  }, [key, nonce])

  const reload = useCallback(() => {
    if (key !== null) cache.delete(key)
    setNonce((n) => n + 1)
  }, [key])

  const stale = snapshot.key !== key
  return {
    data: stale ? undefined : snapshot.data,
    error: stale ? undefined : snapshot.error,
    loading: stale ? key !== null : snapshot.loading,
    reload,
  }
}
