import { useEffect, useMemo, useState } from 'react'
import { DataContext, resolveDataSource, type DataContextValue } from './api/source'
import { ErrorBoundary } from './components/ErrorBoundary'
import { SidePanel } from './components/SidePanel'
import { TopBar } from './components/TopBar'
import { FloorCanvas } from './map/FloorCanvas'
import { createAppState } from './state/reducer'
import { StoreProvider } from './state/store'
import { createInitialUiState } from './state/uiState'

function Shell() {
  return (
    <div className="app">
      <TopBar />
      <main className="main">
        <ErrorBoundary label="도면 캔버스">
          <FloorCanvas />
        </ErrorBoundary>
        <ErrorBoundary label="분석 패널">
          <SidePanel />
        </ErrorBoundary>
      </main>
    </div>
  )
}

function Ready({ context }: { context: DataContextValue }) {
  const initialState = useMemo(() => createAppState(createInitialUiState(context.meta)), [context.meta])
  return (
    <DataContext value={context}>
      <StoreProvider initialState={initialState}>
        <Shell />
      </StoreProvider>
    </DataContext>
  )
}

/** Resolves the data source (live backend or mock fixtures) and renders the Atlas shell. */
export function App({ context: provided }: { context?: DataContextValue }) {
  const [context, setContext] = useState<DataContextValue | null>(provided ?? null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (context) return
    let cancelled = false
    resolveDataSource().then(
      (value) => {
        if (!cancelled) setContext(value)
      },
      (e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      },
    )
    return () => {
      cancelled = true
    }
  }, [context])

  if (error) {
    return (
      <div className="boot error" role="alert">
        Atlas를 시작하지 못했습니다: {error}
      </div>
    )
  }
  if (!context) {
    return (
      <div className="boot" role="status">
        Atlas를 준비하는 중…
      </div>
    )
  }
  return <Ready context={context} />
}
