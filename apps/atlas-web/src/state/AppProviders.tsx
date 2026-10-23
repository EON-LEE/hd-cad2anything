import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { normalizeMeta } from '../api/client'
import { DataContext, type DataContextValue } from '../api/source'
import { createAppState } from './reducer'
import { StoreProvider } from './store'
import { createInitialUiState } from './uiState'

/**
 * Data context (with a refreshable `/api/meta`) and the app store. The initial UI state is built
 * once from the first meta; later meta refreshes (new revisions) do not reset the UI state.
 */
export function AppProviders({ context, children }: { context: DataContextValue; children: ReactNode }) {
  const [meta, setMeta] = useState(context.meta)
  const [initialState] = useState(() => createAppState(createInitialUiState(context.meta)))
  const { source } = context

  const refreshMeta = useCallback(async () => {
    const next = normalizeMeta(await source.getMeta())
    setMeta(next)
    return next
  }, [source])

  const value = useMemo(() => ({ ...context, meta, refreshMeta }), [context, meta, refreshMeta])
  return (
    <DataContext value={value}>
      <StoreProvider initialState={initialState}>{children}</StoreProvider>
    </DataContext>
  )
}
