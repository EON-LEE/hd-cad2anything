import { useEffect, useRef, useState } from 'react'
import { BackendUnavailableError, resolveDataSource, type DataContextValue } from './api/source'
import { BootError } from './components/BootError'
import { ErrorBoundary } from './components/ErrorBoundary'
import { SidePanel } from './components/SidePanel'
import { TopBar } from './components/TopBar'
import { ViewToggle, type MobileView } from './components/ViewToggle'
import { FloorCanvas } from './map/FloorCanvas'
import { AppProviders } from './state/AppProviders'
import { useAppState } from './state/store'

function Shell() {
  const { focusSeq, ui } = useAppState()
  // On the stacked phone layout the map and side panel share the screen; this picks which one shows.
  const [mobileView, setMobileView] = useState<MobileView>('map')

  // Reveal the map when the agent focuses units or newly draws a journey/route/flow, so the result is
  // seen without a manual tap. Inert on desktop, where both panes are always visible.
  useEffect(() => {
    setMobileView('map')
  }, [focusSeq])
  const overlayShown = !!(ui.journey || ui.route || ui.flow)
  const hadOverlay = useRef(overlayShown)
  useEffect(() => {
    if (overlayShown && !hadOverlay.current) setMobileView('map')
    hadOverlay.current = overlayShown
  }, [overlayShown])

  return (
    <div className="app">
      <TopBar />
      <ViewToggle value={mobileView} onChange={setMobileView} />
      <main className={`main view-${mobileView}`}>
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
  return (
    <AppProviders context={context}>
      <Shell />
    </AppProviders>
  )
}

/** Resolves the data source (live backend or mock fixtures) and renders the Atlas shell. */
export function App({ context: provided }: { context?: DataContextValue }) {
  const [context, setContext] = useState<DataContextValue | null>(provided ?? null)
  const [error, setError] = useState<string | null>(null)
  // Bumped by the retry button of the start-up error screen.
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (context) return
    let cancelled = false
    setError(null)
    resolveDataSource().then(
      (value) => {
        if (!cancelled) setContext(value)
      },
      (e: unknown) => {
        if (!cancelled) setError(e instanceof BackendUnavailableError ? e.detail : e instanceof Error ? e.message : String(e))
      },
    )
    return () => {
      cancelled = true
    }
  }, [context, attempt])

  if (error) return <BootError message={error} onRetry={() => setAttempt((n) => n + 1)} />
  if (!context) {
    return (
      <div className="boot" role="status">
        Atlas를 준비하는 중…
      </div>
    )
  }
  return <Ready context={context} />
}
